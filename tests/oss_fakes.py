"""OSS test configuration and an in-memory SDK bucket; no network requests."""

OSS_ENV = {
    "OSS_REGION": "oss-cn-hangzhou",
    "OSS_BUCKET": "test-bucket",
    "OSS_ACCESS_KEY_ID": "test-id",
    "OSS_ACCESS_KEY_SECRET": "test-secret",
}


class MissingObject(Exception):
    status = 404


class FakeResult:
    def __init__(self, headers, body=b""):
        self.headers = headers
        self.body = body
        self.closed = False

    def read(self):
        return self.body

    def close(self):
        self.closed = True


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.last_result = None

    def put_object(self, key, body, headers):
        self.objects[key] = (body, dict(headers, **{"Content-Length": str(len(body))}))

    def head_object(self, key):
        if key not in self.objects:
            raise MissingObject()
        body, headers = self.objects[key]
        return FakeResult(headers, body)

    def get_object(self, key):
        self.last_result = self.head_object(key)
        return self.last_result

    def delete_object(self, key):
        self.objects.pop(key, None)
