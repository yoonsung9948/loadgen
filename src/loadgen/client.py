from .config import TargetConfig, RequestConfig
import httpx

class HTTPClient:
    def __init__(self, target_config: TargetConfig, req_config: RequestConfig) -> None:
        self.url = str(target_config.url)
        self.method = target_config.method
        self.req = req_config.model_copy(deep=True)
        self._client = httpx.AsyncClient()

    async def __aenter__(self):
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self._client.__aexit__(exc_type, exc_value, traceback)

    async def send(self) -> httpx.Response:
        request = self._client.build_request(
            method=self.method,
            url=self.url,
            headers=self.req.headers,
            json=self.req.json_body,
        )
        return await self._client.send(request)
