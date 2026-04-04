import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import ClassVar

import uvicorn
from escpos.printer import Network
from fastapi import FastAPI
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from faststream.rabbit import RabbitQueue
from faststream.rabbit.fastapi import RabbitRouter
from pydantic import AmqpDsn
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic_settings import BaseSettings
from pydantic_settings import JsonConfigSettingsSource
from pydantic_settings import PydanticBaseSettingsSource
from pydantic_settings import SettingsConfigDict
from starlette.responses import Response
from starlette.status import HTTP_204_NO_CONTENT
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR


class Settings(BaseSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        extra="ignore",
        json_file=Path.home() / ".croupier.json",
        json_file_encoding="utf-8",
    )
    queue_url: AmqpDsn
    exchange_name: str
    queue_name: str
    dlx_name: str
    dlq_name: str

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        env_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (JsonConfigSettingsSource(settings_cls),)


class Message(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
    content: bytes
    network_host: str
    network_timeout: int


settings = Settings()  # type: ignore[call-arg]
router = RabbitRouter(settings.queue_url.unicode_string())


@router.subscriber(queue=RabbitQueue(name=settings.queue_name, declare=False))
@router.post("/handle-message")
async def handle_message(body: Message) -> None:  # noqa: RUF029
    printer = Network(
        host=body.network_host,
        timeout=body.network_timeout,
    )
    printer.open()
    printer._raw(body.content)  # noqa: SLF001  # pylint: disable=W0212
    printer.close()


@router.get("/")
async def health(request: Request) -> Response:
    if await request.state.broker.ping(6):
        return Response(status_code=HTTP_204_NO_CONTENT)
    return Response(status_code=HTTP_500_INTERNAL_SERVER_ERROR)


def main() -> None:
    file_handler = RotatingFileHandler(
        filename=Path.home() / ".croupier.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"),
    )
    for name in ("faststream", "faststream.access.rabbit"):
        logging.getLogger(name).addHandler(file_handler)

    app = FastAPI(debug=True)
    app.include_router(router)
    app.add_middleware(
        CORSMiddleware,  # ty:ignore[invalid-argument-type]
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    uvicorn.run(app)
