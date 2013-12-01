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
        """Returns a wrapper for the value referred to by 'key'.

        The Wrapper has a 'value' and 'duration' properties, and a 
        sync method.

        if there was no value found in the cache for that key, the 
        'value' and 'duration' properties can be set for the 'sync' 
        method to save.

        'sync' will have no effect if a value already existed or if 
        the duration property is missing.

        the wrapper behave as a context manager and will try to sync 
        the value when the context exit. If the context exit on a 
        raised APIError, it will try setting the duration from the 
        exception properties.

        """
        return CacheContextManager(self, key, self.get(key))

    def get(self, key):
        """Return the value referred to by 'key' if it is cached.

        key:
            a str.

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
        """Cache the provided value, referenced by 'key', 
        for the given duration.

        key:
            a str.
        value:
            a str (typically the body of the an api response).
        duration:
            a number of seconds before this cache entry should expire.

        """
        expiration = time.time() + duration
        self.cache[key] = (value, expiration)


class CacheContextManager(object):
    """Wrapper for a cached value to help setting one if no value 
    was found.
    
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
        """Set the duration from a result or an APIError."""

        if result.timestamp is None or result.expires is None:
            return
        self.duration = result.expires - result.timestamp

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.sync()
            return

        if not issubclass(exc_type, APIError):
            return

        self.set_duration(exc_value)
        self.sync()


BaseAPIRequest = collections.namedtuple("BaseAPIRequest", [
        "base_url",
        "path",
        "params",
    ])


class APIRequest(BaseAPIRequest):
    """Immutable representation of an api request."""

    def __new__(cls, base_url, path, params):
        """Setup the request base_url, path and params (sorted 
        and cleaned).

        """
        return BaseAPIRequest.__new__(
            cls, 
            base_url,
            path,
            tuple(sorted((k, _clean(v),) for k, v in params)),
        )

    @classmethod
    def from_api(cls, api, path, params=None):
        """Create a request from an API instance, a path and a dict of 
        parameter.

        The api key parameters will be added to the ones provided if 
        the 'api' has an 'api_key' property set.

        """
        params = params or {}

        if api.api_key:
            params['keyID'] = api.api_key[0]
            params['vCode'] = api.api_key[1]

        req = cls(api.base_url, path, params.iteritems())

        _log.debug(
            "Created APIRequest(base_url=%r, path=$r, params=%r)",
            req.base_url,
            req.path,
            tuple(
                (k, v if k != "vCode" else '*' * len(v),) 
                    for k, v in req.params
            )
        )

        return req

    @property
    def encoded_params(self):
        return urlencode(self.params)

    @property
    def absolute_url(self):
        return "https://%s/%s.xml.aspx" % (self.base_url, self.path)


APIResult = collections.namedtuple("APIResult", [
        "result",
        "timestamp",
        "expires",
    ])


class API(object):
    """A wrapper around the EVE API."""

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

    def _set_last_timestamps(self, current_time=0, cached_until=0):
        self.last_timestamps = {
            'current_time': current_time,
            'cached_until': cached_until,
        }

    def _cache_key(self, request):
        # Paradoxically, Shelve doesn't like integer keys.
        # TODO: add base_url to key?
        return '%s-%s' % (
            self.CACHE_VERSION, hash((request.path, request.params,)),
        )

    def process_response(self, response):
        """Extracts from an api response the result (as an ElementTree 
        element), the currentTime and the cachedUntil (as a time stamp) 
        elements.

        Raises an APIError if the API request failed (i.e. if the 
        response has an error element).

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
        request = APIRequest.from_api(self, path, params)
        with self.cache.cache_for(self._cache_key(request)) as response:
            if not response.value:
                response.value = self.send_request(request)
            else:
                _log.debug("Cache hit, returning cached payload")

            result = self.process_response(response.value)
            response.set_duration(result)

        return result

    def send_request(self, request):
        if _has_requests:
            return self.requests_request(request)
        else:
            return self.urllib2_request(request)

    def urllib2_request(self, req):
        try:
            if req.params:
                # POST request
                _log.debug("POSTing request")
                r = urllib2.urlopen(req.absolute_url, req.encoded_params)
            else:
                # GET request
                _log.debug("GETting request")
                r = urllib2.urlopen(req.absolute_url)
            result = r.read()
            r.close()
            return result
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

    def requests_request(self, req):
        session = getattr(self, 'session', None)
        if not session:
            session = requests.Session()
            self.session = session

        try:
            if req.params:
                # POST request
                _log.debug("POSTing request")
                r = session.post(req.absolute_url, params=req.encoded_params)
            else:
                # GET request
                _log.debug("GETting request")
                r = session.get(req.absolute_url)
            return r.content
        except requests.exceptions.RequestException as e:
            # TODO: Handle this better?
            raise e


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
