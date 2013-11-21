from google.appengine.api import memcache
from google.appengine.api import urlfetch
from google.appengine.ext import ndb
from evelink import api
import time

class AppEngineAPIRequest(api.APIRequest):

    def send(self, api):
        result = urlfetch.fetch(
                url=self.absolute_url,
                payload=self.encoded_params,
                method=urlfetch.POST if self.params else urlfetch.GET,
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
                        if self.params else {}
                )
        return result.content


class AppEngineAPI(api.API):
    """Subclass of api.API that is compatible with Google Appengine."""

    Request = AppEngineAPIRequest
    
    def __init__(self, base_url="api.eveonline.com", cache=None, api_key=None):
        cache = cache or AppEngineCache()
        super(AppEngineAPI, self).__init__(base_url=base_url,
                cache=cache, api_key=api_key)


class AppEngineCache(api.APICache):
    """Memcache backed APICache implementation."""
    def get(self, key):
        return memcache.get(key)

    def put(self, key, value, duration):
        if duration < 0:
            duration = time.time() + duration
        memcache.set(key, value, time=duration)


class EveLinkCache(ndb.Model):
    value = ndb.PickleProperty()
    expiration = ndb.IntegerProperty()


class AppEngineDatastoreCache(api.APICache):
    """An implementation of APICache using the AppEngine datastore."""

    def __init__(self):
        super(AppEngineDatastoreCache, self).__init__()

    def get(self, cache_key):
        db_key = ndb.Key(EveLinkCache, cache_key)
        result = db_key.get()
        if not result:
            return None
        if result.expiration < time.time():
            db_key.delete()
            return None
        return result.value

    def put(self, cache_key, value, duration):
        expiration = int(time.time() + duration)
        cache = EveLinkCache.get_or_insert(cache_key)
        cache.value = value
        cache.expiration = expiration
        cache.put()
