import gzip
from StringIO import StringIO
import unittest2 as unittest

import mock
import urllib2

import evelink.api as evelink_api


def compress(s):
    out = StringIO()
    f = gzip.GzipFile(fileobj=out, mode='w')
    f.write(s)
    f.close()
    return out.getvalue()


class HelperTestCase(unittest.TestCase):

    def test_parse_ts(self):
        self.assertEqual(
            evelink_api.parse_ts("2012-06-12 12:04:33"),
            1339502673,
        )

class CacheTestCase(unittest.TestCase):

    def setUp(self):
        self.cache = evelink_api.APICache()

    def test_cache(self):
        self.cache.put('foo', 'bar', 3600)
        self.assertEqual(self.cache.get('foo'), 'bar')

    def test_expire(self):
        self.cache.put('baz', 'qux', -1)
        self.assertEqual(self.cache.get('baz'), None)

    def test_context_with_empty_cache(self):
        with self.cache.cache_for('foo') as foo_cache:
            self.assertEqual(None, foo_cache.value)
            foo_cache.value = 'bar'
            foo_cache.duration = 60
        self.assertEqual('bar', self.cache.get('foo'))

    def test_context_with_cache_value_set(self):
        with self.cache.cache_for('foo') as foo_cache:
            self.assertEqual(None, foo_cache.value)
            foo_cache.value = 'baz'
            foo_cache.duration = 60
        self.assertEqual('baz', self.cache.get('foo'))

    def test_context_with_cache_for_fail_sync(self):
        with self.cache.cache_for('foo') as foo_cache:
            self.assertEqual(None, foo_cache.value)
            # forget to set new value
            foo_cache.duration = 60
        self.assertEqual(None, self.cache.get('foo'))

        with self.cache.cache_for('foo') as foo_cache:
            self.assertEqual(None, foo_cache.value)
            foo_cache.value = 'baz'
            # forget to set duration
        self.assertEqual(None, self.cache.get('foo'))

    def test_context_with_exception(self):
        try:
            with self.cache.cache_for('foo') as foo_cache:
                self.assertEqual(None, foo_cache.value)
                foo_cache.value = 'bar'
                foo_cache.duration = 60
                raise RuntimeError('baz')
        except RuntimeError:
            pass
        self.assertEqual(None, self.cache.get('foo'))

    def test_context_with_APIError(self):
        try:
            with self.cache.cache_for('foo') as foo_cache:
                self.assertEqual(None, foo_cache.value)
                foo_cache.value = 'bar'
                foo_cache.duration = -1
                raise evelink_api.APIError(timestamp=0, expires=60)
        except evelink_api.APIError:
            pass
        self.assertEqual('bar', self.cache.get('foo'))

    def test_context_with_no_duration_in_APIError(self):
        try:
            with self.cache.cache_for('foo') as foo_cache:
                self.assertEqual(None, foo_cache.value)
                foo_cache.value = 'bar'
                raise evelink_api.APIError()
        except evelink_api.APIError:
            pass 
        self.assertEqual(None, self.cache.get('foo'))


class APITestCase(unittest.TestCase):

    def setUp(self):
        self.api = evelink_api.API()
        # force disable requests if enabled.
        self._has_requests = evelink_api._has_requests
        evelink_api._has_requests = False

        self.test_xml = r"""
                <?xml version='1.0' encoding='UTF-8'?>
                <eveapi version="2">
                    <currentTime>2009-10-18 17:05:31</currentTime>
                    <result>
                        <rowset>
                            <row foo="bar" />
                            <row foo="baz" />
                        </rowset>
                    </result>
                    <cachedUntil>2009-11-18 17:05:31</cachedUntil>
                </eveapi>
            """.strip()

        self.error_xml = r"""
                <?xml version='1.0' encoding='UTF-8'?>
                <eveapi version="2">
                    <currentTime>2009-10-18 17:05:31</currentTime>
                    <error code="123">
                        Test error message.
                    </error>
                    <cachedUntil>2009-11-18 19:05:31</cachedUntil>
                </eveapi>
            """.strip()

    def tearDown(self):
        evelink_api._has_requests = self._has_requests

    def test_cache_key(self):
        req = evelink_api.APIRequest.from_api(self.api, 'foo/bar', {})
        assert self.api._cache_key(req)

        req = evelink_api.APIRequest.from_api(self.api, 'foo/bar', {'baz': 'qux'})
        assert self.api._cache_key(req)

        req1 = evelink_api.APIRequest.from_api(self.api, 'foo/bar', {'a':1, 'b':2})
        req2 = evelink_api.APIRequest.from_api(self.api, 'foo/bar', {'b':2, 'a':1})
        self.assertEqual(self.api._cache_key(req1), self.api._cache_key(req2))

    def test_cache_key_variance(self):
        """Make sure that things which shouldn't have the same cache key don't."""
        req1 = evelink_api.APIRequest.from_api(self.api, 'foo/bar', {'a':1})
        req2 = evelink_api.APIRequest.from_api(self.api, 'foo/bar', {'b':2})
        self.assertNotEqual(
            self.api._cache_key(req1),
            self.api._cache_key(req2)
        )

        req1 = evelink_api.APIRequest.from_api(self.api, 'foo/bar', {'a':1})
        req2 = evelink_api.APIRequest.from_api(self.api, 'foo/bar', {'a':2})
        self.assertNotEqual(
            self.api._cache_key(req1),
            self.api._cache_key(req2)
        )

        req1 = evelink_api.APIRequest.from_api(self.api, 'foo/bar', {})
        req2 = evelink_api.APIRequest.from_api(self.api, 'foo/baz', {})
        self.assertNotEqual(
            self.api._cache_key(req1),
            self.api._cache_key(req2)
        )

    @mock.patch('urllib2.urlopen')
    def test_get(self, mock_urlopen):
        # mock up an urlopen compatible response object and pretend to have no
        # cached results; similar pattern for all test_get_* methods below.
        mock_urlopen.return_value.read.return_value = self.test_xml

        result = self.api.get('foo/Bar', {'a':[1,2,3]})

        self.assertEqual(len(result), 3)
        result, current, expiry = result

        rowset = result.find('rowset')
        rows = rowset.findall('row')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].attrib['foo'], 'bar')
        self.assertEqual(self.api.last_timestamps, {
            'current_time': 1255885531,
            'cached_until': 1258563931,
        })
        self.assertEqual(current, 1255885531)
        self.assertEqual(expiry, 1258563931)

    @mock.patch('urllib2.urlopen')
    def test_cached_get(self, mock_urlopen):
        """Make sure that we don't try to call the API if the result is cached."""
        # mock up a urlopen compatible error response, and pretend to have a
        # good test response cached.
        mock_urlopen.return_value.read.return_value = self.error_xml
        
        with mock.patch.object(self.api.cache, 'get') as cache_get:
            cache_get.return_value = self.test_xml
            result = self.api.get('foo/Bar', {'a':[1,2,3]})

        # Ensure this is really not called.
        self.assertFalse(mock_urlopen.called)

        self.assertEqual(len(result), 3)
        result, current, expiry = result

        rowset = result.find('rowset')
        rows = rowset.findall('row')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].attrib['foo'], 'bar')

        # timestamp attempted to be extracted.
        self.assertEqual(self.api.last_timestamps, {
            'current_time': 1255885531,
            'cached_until': 1258563931,
        })
        self.assertEqual(current, 1255885531)
        self.assertEqual(expiry, 1258563931)

    @mock.patch('urllib2.urlopen')
    def test_get_with_apikey(self, mock_urlopen):
        mock_urlopen.return_value.read.return_value = self.test_xml

        self.api.api_key = (1, 'code')

        self.api.get('foo', {'a':[2,3,4]})

        # Make sure the api key id and verification code were passed
        self.assertTrue(mock_urlopen.called)
        self.assertTrue(len(mock_urlopen.call_args[0]) > 0)

        request = mock_urlopen.call_args[0][0]
        self.assertEqual(
            'https://api.eveonline.com/foo.xml.aspx',
            request.get_full_url()
        )
        self.assertEqual(
            'a=2%2C3%2C4&keyID=1&vCode=code',
            request.get_data()
        )

    @mock.patch('urllib2.urlopen')
    def test_get_with_error(self, mock_urlopen):
        # I had to go digging in the source code for urllib2 to find out
        # how to manually instantiate HTTPError instances. :( The empty
        # dict is the headers object.
        def raise_http_error(*args, **kw):
            raise urllib2.HTTPError(
                "http://api.eveonline.com/eve/Error",
                404,
                "Not found!",
                {},
                StringIO(self.error_xml)
            )
        mock_urlopen.side_effect = raise_http_error

        self.assertRaises(evelink_api.APIError,
            self.api.get, 'eve/Error')
        self.assertEqual(self.api.last_timestamps, {
            'current_time': 1255885531,
            'cached_until': 1258571131,
        })

    @mock.patch('urllib2.urlopen')
    def test_get_with_compressed_error(self, mock_urlopen):
        # I had to go digging in the source code for urllib2 to find out
        # how to manually instantiate HTTPError instances. :( The empty
        # dict is the headers object.
        def raise_http_error(*args, **kw):
            raise urllib2.HTTPError(
                "http://api.eveonline.com/eve/Error",
                404,
                "Not found!",
                {'Content-Encoding': 'gzip'},
                StringIO(compress(self.error_xml))
            )
        mock_urlopen.side_effect = raise_http_error

        self.assertRaises(evelink_api.APIError,
            self.api.get, 'eve/Error')
        self.assertEqual(self.api.last_timestamps, {
            'current_time': 1255885531,
            'cached_until': 1258571131,
        })

    @mock.patch('urllib2.urlopen')
    def test_cached_get_with_error(self, mock_urlopen):
        """Make sure that we don't try to call the API if the result is cached."""
        # mocked response is good now, with the error response cached.
        mock_urlopen.return_value.read.return_value = self.test_xml

        with mock.patch.object(self.api.cache, 'get') as cache_get:
            cache_get.return_value = self.error_xml
            self.assertRaises(evelink_api.APIError,
                self.api.get, 'foo/Bar', {'a':[1,2,3]})

        self.assertFalse(mock_urlopen.called)
        self.assertEqual(self.api.last_timestamps, {
            'current_time': 1255885531,
            'cached_until': 1258571131,
        })

    @mock.patch('urllib2.urlopen')
    def test_get_request_compress_response(self, mock_urlopen):
        mock_urlopen.return_value.read.return_value = compress(self.test_xml)
        mock_urlopen.return_value.info.return_value.get.return_value = 'gzip'

        result = self.api.get('foo/Bar', {'a':[1,2,3]})
        self.assertTrue(mock_urlopen.called)
        self.assertTrue(len(mock_urlopen.call_args[0]) > 0)
        self.assertEqual(
            'gzip', 
            mock_urlopen.call_args[0][0].get_header('Accept-encoding')
        )

        self.assertEqual(len(result), 3)
        result, current, expiry = result

        rowset = result.find('rowset')
        rows = rowset.findall('row')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].attrib['foo'], 'bar')
        self.assertEqual(self.api.last_timestamps, {
            'current_time': 1255885531,
            'cached_until': 1258563931,
        })
        self.assertEqual(current, 1255885531)
        self.assertEqual(expiry, 1258563931)

class AutoCallTestCase(unittest.TestCase):

    def test_python_func(self):
        def func(a, b, c=None, d=None):
            return a, b, c, d

        self.assertEqual((1, 2, 3, 4,), func(1, 2, c=3, d=4))
        self.assertEqual((1, 2, 3, 4,), func(a=1, b=2, c=3, d=4))
        self.assertEqual((1, 2, 3, 4,), func(c=3, a=1, b=2, d=4))
        self.assertEqual((1, 2, 3, 4,), func(1, b=2, c=3, d=4))
        self.assertRaises(TypeError, func, 2, a=1, c=3, d=4)

    def test_translate_args(self):
        args = {'foo': 'bar'}
        mapping = {'foo': 'baz'}
        self.assertEqual(
            {'baz': 'bar'}, 
            evelink_api.translate_args(args, mapping)
        )

    def test_get_args_and_defaults(self):
        def target(a, b, c=None, d=None):
            pass
        args_specs, defaults = evelink_api.get_args_and_defaults(target)
        self.assertEqual(['a', 'b', 'c', 'd'], args_specs)
        self.assertEqual({'c': None, 'd': None}, defaults)

    def test_map_func_args(self):
        args = [1, 2]
        kw = {'c': 3, 'd': 4}
        args_names = ('a', 'b', 'c', 'd',)
        defaults = {'c': None, 'd': None}
        map_ = evelink_api.map_func_args(args, kw, args_names, defaults)
        self.assertEqual({'a': 1, 'b': 2, 'c': 3, 'd': 4}, map_)

    def test_map_func_args_with_default(self):
        args = [1, 2]
        kw = {'c': 3}
        args_names = ('a', 'b', 'c', 'd',)
        defaults = {'c': None, 'd': None}
        map_ = evelink_api.map_func_args(args, kw, args_names, defaults)
        self.assertEqual({'a': 1, 'b': 2, 'c': 3, 'd': None}, map_)

    def test_map_func_args_with_all_positional_arguments(self):
        args = [1, 2, 3, 4]
        kw = {}
        args_names = ('a', 'b', 'c', 'd',)
        defaults = {'c': None, 'd': None}
        map_ = evelink_api.map_func_args(args, kw, args_names, defaults)
        self.assertEqual({'a': 1, 'b': 2, 'c': 3, 'd': 4}, map_)

    def test_map_func_args_with_too_many_argument(self):
        args = [1, 2, 3]
        kw = {'c': 4, 'd': 5}
        args_names = ('a', 'b', 'c', 'd',)
        defaults = {'c': None, 'd': None}
        self.assertRaises(
            TypeError,
            evelink_api.map_func_args,
            args,
            kw,
            args_names,
            defaults
        )

    def test_map_func_args_with_twice_same_argument(self):
        args = [2]
        kw = {'a': 1, 'c': 3, 'd': 4}
        args_names = ('a', 'b', 'c', 'd',)
        defaults = {'c': None, 'd': None}
        self.assertRaises(
            TypeError,
            evelink_api.map_func_args,
            args,
            kw,
            args_names,
            defaults
        )

    def test_map_func_args_with_too_few_args(self):
        args = [1, ]
        kw = {'c': 3, 'd': 4}
        args_names = ('a', 'b', 'c', 'd',)
        defaults = {'c': None, 'd': None}
        self.assertRaises(
            TypeError,
            evelink_api.map_func_args,
            args,
            kw,
            args_names,
            defaults
        )

    def test_deco_add_request_specs(self):
        
        @evelink_api.auto_call('foo/bar')
        def func(self, char_id, limit=None, before_kill=None, api_result=None):
            pass

        self.assertEqual(
            {
                'path': 'foo/bar',
                'args': [
                    'char_id', 'limit', 'before_kill'
                ],
                'defaults': dict(limit=None, before_kill=None),
                'prop_to_param': tuple(),
                'map_params': {}
            },
            func._request_specs
            )

    def test_call_wrapped_method(self):
        repeat = mock.Mock()
        client = mock.Mock(name='foo')

        @evelink_api.auto_call(
            'foo/bar', 
            map_params={'char_id': 'id', 'limit': 'limit', 'before_kill': 'prev'}
        )
        def func(self, char_id, limit=None, before_kill=None, api_result=None):
            repeat(
                self, char_id, limit=limit,
                before_kill=before_kill, api_result=api_result
            )

        func(client, 1, limit=2, before_kill=3)
        repeat.assert_called_once_with(
            client, 1, limit=2, before_kill=3, api_result=client.api.get.return_value
        )
        client.api.get.assert_called_once_with(
            'foo/bar',
            params={'id':1, 'prev': 3, 'limit': 2}
        )

    def test_call_wrapped_method_raise_key_error(self):
        repeat = mock.Mock()
        client = mock.Mock(name='foo')

        @evelink_api.auto_call('foo/bar')
        def func(self, char_id, api_result=None):
            repeat(self, char_id)

        # TODO: raise error when decorating the method
        self.assertRaises(KeyError, func, client, 1)

    def test_call_wrapped_method_none_arguments(self):
        repeat = mock.Mock()
        client = mock.Mock(name='foo')

        @evelink_api.auto_call(
            'foo/bar', map_params={'char_id': 'char_id', 'limit': 'limit'}
        )
        def func(self, char_id, limit=None, api_result=None):
            repeat(self, char_id, limit=limit, api_result=api_result)

        func(client, 1)
        repeat.assert_called_once_with(
            client, 1, limit=None, api_result=client.api.get.return_value
        )
        client.api.get.assert_called_once_with(
            'foo/bar',
            params={'char_id':1}
        )

    def test_call_wrapped_method_with_properties(self):
        repeat = mock.Mock()
        client = mock.Mock(name='client')
        client.char_id = 1

        @evelink_api.auto_call(
            'foo/bar',
            prop_to_param=('char_id',),
            map_params={'char_id': 'char_id', 'limit': 'limit'}
        )
        def func(self, limit=None, api_result=None):
            repeat(
                self, 
                limit=limit, api_result=api_result
            )

        func(client, limit=2)
        repeat.assert_called_once_with(
            client, limit=2, api_result=client.api.get.return_value
        )
        client.api.get.assert_called_once_with(
            'foo/bar',
            params={'char_id':1, 'limit': 2}
        )

    def test_call_wrapped_method_with_api_result(self):
        repeat = mock.Mock()
        client = mock.Mock(name='client')
        results = mock.Mock(name='APIResult')

        @evelink_api.auto_call('foo/bar')
        def func(self, char_id, limit=None, before_kill=None, api_result=None):
            repeat(
                self, char_id, limit=limit,
                before_kill=before_kill, api_result=api_result
            )

        func(client, 1, limit=2, before_kill=3, api_result=results)
        repeat.assert_called_once_with(
            client, 1, limit=2, before_kill=3, api_result=results
        )
        self.assertFalse(client.get.called)


if __name__ == "__main__":
    unittest.main()
