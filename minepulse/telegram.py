"""Minimal Telegram Bot API long-polling client using Python's standard library."""
from __future__ import annotations
import asyncio
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


class Telegram:
    def __init__(self, token: str):
        self.url = f'https://api.telegram.org/bot{token}/'

    @staticmethod
    def _sync_request(url: str, params: dict, timeout: float) -> object:
        data = json.dumps(params, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        req = Request(url, data=data, headers={'Content-Type': 'application/json; charset=utf-8'})
        try:
            with urlopen(req, timeout=timeout) as resp:
                payload = json.load(resp)
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read())
            except (ValueError, OSError):
                raise TelegramError(f'HTTP {exc.code}', exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TelegramError('Нет связи с Telegram API') from exc
        if not payload.get('ok'):
            raise TelegramError(str(payload.get('description', 'Ошибка Telegram')),
                                int(payload.get('error_code', 0)))
        return payload['result']

    async def call(self, method: str, **params):
        # The token is stored only in config; do not include complete URLs in logs.
        timeout = 40 if method == 'getUpdates' else 15
        return await asyncio.to_thread(self._sync_request, self.url + method, params, timeout)

    async def message(self, chat_id: int, text: str, keyboard: list | None = None,
                      edit_id: int | None = None):
        params = dict(chat_id=chat_id, text=text, parse_mode='HTML',
                      disable_web_page_preview=True)
        if keyboard is not None:
            params['reply_markup'] = {'inline_keyboard': keyboard}
        if edit_id is not None:
            try:
                return await self.call('editMessageText', message_id=edit_id, **params)
            except TelegramError as exc:
                if 'message is not modified' in str(exc).lower():
                    return None
                LOG.info('Cannot edit message; sending new one: %s', exc)
        return await self.call('sendMessage', **params)

    async def answer(self, callback_id: str, text: str = ''):
        try:
            await self.call('answerCallbackQuery', callback_query_id=callback_id, text=text[:200])
        except TelegramError:
            pass


def btn(text: str, data: str) -> dict:
    return {'text': text[:64], 'callback_data': data[:64]}
