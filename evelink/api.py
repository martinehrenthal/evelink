import calendar
import collections
import functools
import logging
from operator import itemgetter
import re
import time
from urllib import urlencode
import urllib2
from xml.etree import ElementTree

_log = logging.getLogger('evelink.api')

try:
    import requests
    _has_requests = True
except ImportError:
    _log.info('`requests` not available, falling back to urllib2')
    _has_requests = None

def _clean(v):
    """Convert parameters into an acceptable format for the API."""
    if isinstance(v, (list, set, tuple)):
        return ",".join(str(i) for i in v)
    else:
        return str(v)


def parse_ts(v):
    """Parse a timestamp from EVE API XML into a unix-ish timestamp."""
    if v == '':
        return None
    ts = calendar.timegm(time.strptime(v, "%Y-%m-%d %H:%M:%S"))
    # Deal with EVE's nonexistent 0001-01-01 00:00:00 timestamp
    return ts if ts > 0 else None


def get_named_value(elem, field):
    """Returns the string value of the named child element."""
    try:
        return elem.find(field).text
    except AttributeError:
        return None


def get_ts_value(elem, field):
    """Returns the timestamp value of the named child element."""
    val = get_named_value(elem, field)
    if val:
        return parse_ts(val)
    return None


def get_int_value(elem, field):
    """Returns the integer value of the named child element."""
    val = get_named_value(elem, field)
    if val:
        return int(val)
    return val


def get_float_value(elem, field):
    """Returns the float value of the named child element."""
    val = get_named_value(elem, field)
    if val:
        return float(val)
    return val


def get_bool_value(elem, field):
    """Returns the boolean value of the named child element."""
    val = get_named_value(elem, field)
    if val == 'True':
        return True
    elif val == 'False':
        return False
    return None


def elem_getters(elem):
    """Returns a tuple of (_str, _int, _float, _bool, _ts) functions.

    These are getters closed around the provided element.
    """
    _str = lambda key: get_named_value(elem, key)
    _int = lambda key: get_int_value(elem, key)
    _float = lambda key: get_float_value(elem, key)
    _bool = lambda key: get_bool_value(elem, key)
    _ts = lambda key: get_ts_value(elem, key)

    return _str, _int, _float, _bool, _ts


def parse_keyval_data(data_string):
    """Parse 'key: value' lines from a LF-delimited string."""
    keyval_pairs = data_string.strip().split('\n')
    results = {}
    for pair in keyval_pairs:
        key, _, val = pair.strip().partition(': ')

        if 'Date' in key:
            val = parse_ms_date(val)
        elif val == 'null':
            val = None
        elif re.match(r"^-?\d+$", val):
            val = int(val)
        elif re.match(r"-?\d+\.\d+", val):
            val = float(val)

        results[key] = val
    return results

def parse_ms_date(date_string):
    """Convert MS date format into epoch"""

    return int(date_string)/10000000 - 11644473600;

class APIError(Exception):
    """Exception raised when the EVE API returns an error."""

    def __init__(self, code=None, message=None, timestamp=None, expires=None):
        self.code = code
        self.message = message
        self.timestamp = timestamp
        self.expires = expires

    def __repr__(self):
        return "APIError(%r, %r, timestamp=%r, expires=%r)" % (
            self.code, self.message, self.timestamp, self.expires)

    def __str__(self):
        return "%s (code=%d)" % (self.message, int(self.code))

class APICache(object):
    """Minimal interface for caching API requests.

    This very basic implementation simply stores values in
    memory, with no other persistence. You can subclass it
    to define a more complex/featureful/persistent cache.
    """

    def __init__(self):
        self.cache = {}

    def cache_for(self, key):
        return CacheContextManager(self, key, self.get(key))

    def get(self, key):
        """Return the value referred to by 'key' if it is cached.

        key:
            a result from the Python hash() function.
        """
        result = self.cache.get(key)
        if not result:
            return None
        value, expiration = result
        if expiration < time.time():
            del self.cache[key]
            return None
        return value

    def put(self, key, value, duration):
        """Cache the provided value, referenced by 'key', for the given duration.

        key:
            a result from the Python hash() function.
        value:
            an xml.etree.ElementTree.Element object
        duration:
            a number of seconds before this cache entry should expire.
        """
        expiration = time.time() + duration
        self.cache[key] = (value, expiration)


class CacheContextManager(object):
    """Helper to return the cached value or to set one if no value was found.
    
    """

    def __init__(self, cache, key, initial_value, duration=None):
        self.cache = cache
        self._key = key
        self.value = self._old_value = initial_value
        self.duration = duration

    def sync(self):
        # the cache is set already
        if self._old_value is not None:
            return

        # either the value or the duration missing;
        # the cache value cannot be set
        if self.duration is None or self.value is None:
            return

        self.cache.put(self._key, self.value, self.duration)
        self._old_value = self.value

    def set_duration(self, result):
        """Set the duration from a result or an APIError.

        """
        if result.timestamp is None or result.expires is None:
            return
        self.duration = result.expires - result.timestamp

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.sync()

        if exc_type is not APIError:
            return

        self.set_duration(exc_value)
        self.sync()


class APIRequest(tuple):
    """
    Immutable representation of an api request.

    """

    def __new__(cls, api, path, params=None):
        params = params or {}

        for key in params:
            params[key] = _clean(params[key])

        _log.debug("Calling %s with params=%r", path, params)

        if api.api_key:
            _log.debug("keyID and vCode added")
            params['keyID'] = api.api_key[0]
            params['vCode'] = api.api_key[1]

        return tuple.__new__(
            cls, 
            (
                api.base_url,
                path,
                tuple(sorted(params.iteritems())),
            )
        )

    base_url = property(itemgetter(0))
    path = property(itemgetter(1))
    params = property(itemgetter(2))


    @property
    def encoded_params(self):
        return urlencode(self.params)

    @property
    def absolute_url(self):
        return "https://%s/%s.xml.aspx" % (self.base_url, self.path)

    def send(self, api):
        """
        Send the request and return the body as a string.

        """
        try:
            if self.params:
                # POST request
                _log.debug("POSTing request")
                r = urllib2.urlopen(self.absolute_url, self.encoded_params)
            else:
                # GET request
                _log.debug("GETting request")
                r = urllib2.urlopen(self.absolute_url)
        except urllib2.HTTPError as r:
            # urllib2 handles non-2xx responses by raising an exception that
            # can also behave as a file-like object. The EVE API will return
            # non-2xx HTTP codes on API errors (since Odyssey, apparently)
            pass
        except urllib2.URLError as e:
            # TODO: Handle this better?
            raise e
        
        try:
            return r.read()
        finally:
            r.close()


class APIRequestRequests(APIRequest):
    
    def send(self, api):
        if api.session is None:
            api.session = requests.Session()

        try:
            if self.params:
                # POST request
                _log.debug("POSTing request")
                r = api.session.post(
                    self.absolute_url,
                    params=self.encoded_params
                )
            else:
                # GET request
                _log.debug("GETting request")
                r = api.session.get(self.absolute_url)
            return r.content
        except requests.exceptions.RequestException as e:
            # TODO: Handle this better?
            raise e


APIResult = collections.namedtuple("APIResult", [
        "result",
        "timestamp",
        "expires",
    ])


class API(object):
    """A wrapper around the EVE API."""

    if _has_requests:
        Request = APIRequestRequests
    else:
        Request = APIRequest

    def __init__(self, base_url="api.eveonline.com", cache=None, api_key=None):
        self.base_url = base_url

        cache = cache or APICache()
        if not isinstance(cache, APICache):
            raise ValueError("The provided cache must subclass from APICache.")
        self.cache = cache
        self.CACHE_VERSION = '1'

        if api_key and len(api_key) != 2:
            raise ValueError("The provided API key must be a tuple of (keyID, vCode).")
        self.api_key = api_key
        self._set_last_timestamps()

        self.session = None

    def _set_last_timestamps(self, current_time=0, cached_until=0):
        self.last_timestamps = {
            'current_time': current_time,
            'cached_until': cached_until,
        }

    def _cache_key(self, request):
        # Paradoxically, Shelve doesn't like integer keys.
        return '%s-%s' % (self.CACHE_VERSION, hash(request[1:]),)

    def process_response(self, response):
        """Extracts from an api response the result (as an ElementTree 
        element), the currentTime and the cachedUntil (as a time stamp) 
        elements.

        Raises an APIError if the API request failed (i.e. if the response 
        has an error element).

        """
        tree = ElementTree.fromstring(response)
        current_time = get_ts_value(tree, 'currentTime')
        expires_time = get_ts_value(tree, 'cachedUntil')

        self._set_last_timestamps(current_time, expires_time)

        error = tree.find('error')
        if error is not None:
            code = error.attrib['code']
            message = error.text.strip()
            exc = APIError(code, message, current_time, expires_time)
            _log.error("Raising API error: %r" % exc)
            raise exc

        return APIResult(tree.find('result'), current_time, expires_time)

    def get(self, path, params=None):
        """Request a specific path from the EVE API.

        The supplied path should be a slash-separated path
        frament, e.g. "corp/AssetList". (Basically, the portion
        of the API url in between the root / and the .xml bit.).

        Raises an APIError if the API request failed.

        """
        req = self.Request(self, path, params)
        with self.cache.cache_for(self._cache_key(req)) as response:
            if not response.value:
                response.value = req.send(self)
            else:
                _log.debug("Cache hit, returning cached payload")

            result = self.process_response(response.value)
            response.set_duration(result)

        return result


def auto_api(func):
    """A decorator to automatically provide an API instance.

    Functions decorated with this will have the api= kwarg
    automatically supplied with a default-initialized API()
    object if no other API object is supplied.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if 'api' not in kwargs:
            kwargs['api'] = API()
        return func(*args, **kwargs)
    return wrapper


# vim: set ts=4 sts=4 sw=4 et:
