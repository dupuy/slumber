import posixpath
try:
    from urllib.parse import urlsplit, urlunsplit
except ImportError:
    from urlparse import urlsplit, urlunsplit
from six import u, b, PY3, iteritems, string_types, text_type

import requests

from slumber import exceptions
from slumber.serialize import Serializer

__all__ = ["Resource", "API"]


def url_join(base, *args):
    """
    Helper function to join an arbitrary number of url segments together.
    """
    # Python2 urlsplit can't handle bytearray (TypeError: unhashable type)
    if isinstance(base, bytearray):
        base = bytes(base)

    if isinstance(base, bytes):
        needbytes = True
    else:
        needbytes = False

    try:
        scheme, netloc, path, query, fragment = urlsplit(base)
    except UnicodeDecodeError:
        # PY3 urlsplit uses implicit (ASCII) encoding to decode bytes, but we
        # use latin1 since re-encoding after urlsplit exactly reverses decode
        # for any ASCII superset (needed  for posixpath.join to work,
        # since EBCDIC codes / as \x61 [a]).  This "trick" allows use of ASCII
        # supersets for bytes URL in the original base.
        base = base.decode('latin1')
        scheme, netloc, path, query, fragment = (x.encode('latin1')
                                                 for x in urlsplit(base))
    if not len(path):
        if needbytes:
            path = b('/')
        else:
            path = u('/')
    newargs = []
    try:
        for x in args:
            if needbytes:
                # Although they don't need conversion, bytes args must be ASCII
                # as we cannot know they use the same encoding as base URL.
                if isinstance(x, bytes) or isinstance(x, bytearray):
                    newargs.append(x.decode('ascii').encode('ascii'))
                else:
                    if not isinstance(x, text_type):
                        x = '%s' % x
                    newargs.append(x.encode('ascii'))
            else:
                if isinstance(x, bytes) or isinstance(x, bytearray):
                    newargs.append(x.decode('ascii'))
                else:
                    if not isinstance(x, text_type):
                        x = '%s' % x
                    newargs.append(x)
        path = posixpath.join(path, *newargs)
        if PY3 and needbytes:
            # PY3 urlunsplit uses implicit (ASCII) encoding to decode bytes,
            # but we use latin1 since re-encoding after urlunsplit exactly
            # reverses decode for any ASCII superset (needed for posixpath.join
            # to work, since EBCDIC codes / as \x61 [a]).  This "trick" allows
            # use of ASCII supersets for bytes URLs (but not args, for safety).
            return urlunsplit([x.decode('latin1') for x in
                               [scheme, netloc, path, query, fragment]]
                              ).encode('latin1')
    except UnicodeError:
        raise TypeError("Can't mix non-ASCII bytes and strings in URL paths.")
    return urlunsplit([scheme, netloc, path, query, fragment])


class ResourceAttributesMixin(object):
    """
    A Mixin that makes it so that accessing an undefined attribute on a class
    results in returning a Resource Instance. This Instance can then be used
    to make calls to the a Resource.

    It assumes that a Meta class exists at self._meta with all the required
    attributes.
    """

    def __getattr__(self, item):
        if item.startswith("_"):
            raise AttributeError(item)

        kwargs = {}
        for key, value in iteritems(self._store):
            kwargs[key] = value

        kwargs.update({"base_url": url_join(self._store["base_url"], item)})

        return Resource(**kwargs)


class Resource(ResourceAttributesMixin, object):
    """
    Resource provides the main functionality behind slumber. It handles the
    attribute -> url, kwarg -> query param, and other related behind the scenes
    python to HTTP transformations. It's goal is to represent a single resource
    which may or may not have children.

    It assumes that a Meta class exists at self._meta with all the required
    attributes.
    """

    def __init__(self, *args, **kwargs):
        self._store = kwargs

    def __call__(self, id=None, format=None, url_override=None):
        """
        Returns a new instance of self modified by one or more of the available
        parameters. These allows us to do things like override format for a
        specific request, and enables the api.resource(ID).get() syntax to get
        a specific resource by it's ID.
        """

        # Short Circuit out if the call is empty
        if id is None and format is None and url_override is None:
            return self

        kwargs = {}
        for key, value in iteritems(self._store):
            kwargs[key] = value

        if id is not None:
            kwargs["base_url"] = url_join(self._store["base_url"], id)

        if format is not None:
            kwargs["format"] = format

        if url_override is not None:
            # @@@ This is hacky and we should probably figure out a better way
            #    of handling the case when a POST/PUT doesn't return an object
            #    but a Location to an object that we need to GET.
            kwargs["base_url"] = url_override

        kwargs["session"] = self._store["session"]

        return self.__class__(**kwargs)

    def _request(self, method, data=None, files=None, params=None):
        s = self._store["serializer"]
        url = self._store["base_url"]

        if self._store["append_slash"] and not url.endswith("/"):
            url = url + "/"

        headers = {"accept": s.get_content_type()}

        if not files:
            headers["content-type"] = s.get_content_type()
            if data is not None:
                data = s.dumps(data)

        resp = self._store["session"].request(method, url, data=data, params=params, files=files, headers=headers)

        if 400 <= resp.status_code <= 499:
            raise exceptions.HttpClientError("Client Error %s: %s" % (resp.status_code, url), response=resp, content=resp.content)
        elif 500 <= resp.status_code <= 599:
            raise exceptions.HttpServerError("Server Error %s: %s" % (resp.status_code, url), response=resp, content=resp.content)

        self._ = resp

        return resp

    def _handle_redirect(self, resp, **kwargs):
        # @@@ Hacky, see description in __call__
        resource_obj = self(url_override=resp.headers["location"])
        return resource_obj.get(params=kwargs)

    def _try_to_serialize_response(self, resp):
        s = self._store["serializer"]

        if resp.headers.get("content-type", None):
            content_type = resp.headers.get("content-type").split(";")[0].strip()

            try:
                stype = s.get_serializer(content_type=content_type)
            except exceptions.SerializerNotAvailable:
                return resp.content

            return stype.loads(resp.content)
        else:
            return resp.content

    def get(self, **kwargs):
        resp = self._request("GET", params=kwargs)
        if 200 <= resp.status_code <= 299:
            return self._try_to_serialize_response(resp)
        else:
            return  # @@@ We should probably do some sort of error here? (Is this even possible?)

    def post(self, data=None, files=None, **kwargs):
        s = self._store["serializer"]

        resp = self._request("POST", data=data, files=files, params=kwargs)
        if 200 <= resp.status_code <= 299:
            return self._try_to_serialize_response(resp)
        else:
            # @@@ Need to be Some sort of Error Here or Something
            return

    def patch(self, data=None, files=None, **kwargs):
        s = self._store["serializer"]

        resp = self._request("PATCH", data=data, files=files, params=kwargs)
        if 200 <= resp.status_code <= 299:
            return self._try_to_serialize_response(resp)
        else:
            # @@@ Need to be Some sort of Error Here or Something
            return

    def put(self, data=None, files=None, **kwargs):
        resp = self._request("PUT", data=data, files=files, params=kwargs)

        if 200 <= resp.status_code <= 299:
            return self._try_to_serialize_response(resp)
        else:
            return False

    def delete(self, **kwargs):
        resp = self._request("DELETE", params=kwargs)
        if 200 <= resp.status_code <= 299:
            if resp.status_code == 204:
                return True
            else:
                return True  # @@@ Should this really be True?
        else:
            return False


class API(ResourceAttributesMixin, object):

    def __init__(self, base_url=None, auth=None, format=None, append_slash=True, session=None, serializer=None):
        if serializer is None:
            serializer = Serializer(default=format)

        if session is None:
            session = requests.session()
            session.auth = auth

        self._store = {
            "base_url": base_url,
            "format": format if format is not None else "json",
            "append_slash": append_slash,
            "session": session,
            "serializer": serializer,
        }

        # Do some Checks for Required Values
        if self._store.get("base_url") is None:
            raise exceptions.ImproperlyConfigured("base_url is required")
