import asyncio
import json
import traceback
from typing import Any, Dict, List, Literal, Optional, cast
from nonebot.typing import overrides
from nonebot.exception import ActionFailed, WebSocketClosed
from nonebot.drivers import (
    URL,
    Driver,
    ForwardDriver,
    Request,
    ReverseDriver,
    WebSocket
)
from nonebot.adapters import Adapter as BaseAdapter

from .bot import Bot
from .event import Event
from .config import Config
from .utils import (
    Log as log,
    MiraiDataclassEncoder,
    SyncIDStore,
    process_event
)

RECONNECT_INTERVAL = 3.0


class Adapter(BaseAdapter):

    @overrides(BaseAdapter)
    def __init__(self, driver: Driver, **kwargs: Any):
        super().__init__(driver, **kwargs)
        self.mirai_config: Config = Config(**self.config.dict())
        self.connections: Dict[str, WebSocket] = {}
        self.tasks: List['asyncio.Task'] = []
        self.setup()

    @classmethod
    @overrides(BaseAdapter)
    def get_name(cls) -> str:
        return 'mirai V2'

    def setup(self) -> None:
        self.driver.on_startup(self.start_forward)
        self.driver.on_shutdown(self.stop_forward)

    async def start_forward(self) -> None:
        for qq in self.mirai_config.mirai_qq:
            try:
                self.tasks.append(asyncio.create_task(self._forward_ws(qq)))
            except Exception as e:
                log.warn(e)

    async def stop_forward(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _forward_ws(self, self_id: int) -> None:
        headers = {
            'verifyKey': self.mirai_config.verify_key,
            'qq': self_id
        }
        request = Request(
            'GET',
            URL('ws://{host}:{port}/all'.format(
                host=self.mirai_config.mirai_host,
                port=self.mirai_config.mirai_port
            )),
            headers=headers,
            timeout=30.0
        )

        bot: Optional[Bot] = None

        while True:
            try:
                async with self.websocket(request) as ws:
                    log.debug(
                        'WebSocket Connection to '
                        f'ws://{self.mirai_config.mirai_host}:{self.mirai_config.mirai_port}/all?'
                        f'qq={self_id} established'
                    )
                    data = await ws.receive()
                    json_data = json.loads(data)
                    if 'data' in json_data and json_data['data']['code'] > 0:
                        log.warn(f'{json_data["data"]["msg"]}: {self_id}')
                        return
                    self.connections[str(self_id)] = ws

                    bot = Bot(self, str(self_id))
                    self.bot_connect(bot)

                    try:
                        while True:
                            data = await ws.receive()
                            json_data = json.loads(data)
                            if int(json_data.get('syncId') or '0') >= 0:
                                SyncIDStore.add_response(json_data)
                                continue
                            asyncio.create_task(process_event(
                                bot,
                                event=Event.new({
                                    **json_data['data'],
                                    'self_id': self_id
                                })
                            ))
                    except WebSocketClosed as e:
                        log.warn(e)
                    except Exception as e: # noqa
                        log.warn(traceback.format_exc())
                    finally:
                        if bot:
                            self.connections.pop(bot.self_id, None)
                            self.bot_disconnect(bot)
                            bot = None
            except Exception as e:
                log.warn(e)

            await asyncio.sleep(RECONNECT_INTERVAL)

    @overrides(BaseAdapter)
    async def _call_api(
        self,
        bot: Bot,
        api: str,
        subcommand: Optional[Literal['get', 'update']] = None,
        **data
    ) -> Any:

        def snake_to_camel(name: str):
            first, *rest = name.split('_')
            return ''.join([first.lower(), *(r.title() for r in rest)])

        sync_id = SyncIDStore.get_id()
        api = snake_to_camel(api)
        data = {snake_to_camel(k): v for k, v in data.items()}
        body = {
            'syncId': sync_id,
            'command': api,
            'subcommand': subcommand,
            'content': {
                **data,
            }
        }

        await cast(WebSocket, self.connections[str(bot.self_id)]).send(
            json.dumps(
                body,
                cls=MiraiDataclassEncoder
            )
        )

        result: Dict[str, Any] = await SyncIDStore.fetch_response(
            sync_id, timeout=self.config.api_timeout)

        if ('data' not in result) or (result['data'].get('code') != 0):
            raise ActionFailed(
                f'{self.get_name()} | {result.get("data") or result}'
            )

        return result['data']
