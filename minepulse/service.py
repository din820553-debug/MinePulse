"""Update handlers, catalog UX, monitoring, subscriptions and Stars promotion."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from html import escape
import logging
import secrets
import time
from typing import Any

from .config import Config
from .database import CATEGORIES, Database
from .probe import Endpoint, Status, AddressError, check_server, parse_endpoint
from .telegram import Telegram, TelegramError, btn

LOG = logging.getLogger(__name__)

MAIN_KB = [
    [btn('🔎 Каталог серверов', 'catalog:all:all:0')],
    [btn('⭐ Избранное', 'favorites'), btn('🧱 Добавить сервер', 'add')],
    [btn('📡 Мои серверы', 'mine'), btn('📊 Статистика', 'stats')],
    [btn('❔ Помощь', 'help')],
]
EDITION_KB = [[btn('☕ Java Edition', 'new:java'), btn('📱 Bedrock Edition', 'new:bedrock')],
              [btn('↩️ В меню', 'menu')]]
CAT_KB = [[btn(title, 'choose:' + key)] for key, title in CATEGORIES.items()]
CAT_KB.append([btn('✖ Отмена', 'menu')])


def bound(text: str, max_length: int) -> str:
    return escape(str(text)[:max_length], quote=False)


def user_id(update: dict) -> int:
    return int(update['from']['id'])


def endpoint_for(server: dict) -> Endpoint:
    return Endpoint(server['edition'], server['host'], server['port'])


def short(server: dict) -> str:
    status = '🟢' if server['status'] == 'online' else ('🔴' if server['status'] == 'offline' else '⚪')
    name = server['name'][:31]
    return f'{status} {name} ({server["edition"].capitalize()})'


@dataclass
class Pending:
    stage: str
    created: float
    edition: str = ''
    address: str = ''
    category: str = ''


class MinePulse:
    def __init__(self, config: Config, db: Database, telegram: Telegram):
        self.config = config
        self.db = db
        self.tg = telegram
        self.pending: dict[int, Pending] = {}
        self.cooldowns: dict[tuple[int, int], float] = {}
        self.user_cooldowns: dict[int, float] = {}
        self.semaphore = asyncio.Semaphore(config.concurrency)

    async def send(self, chat: int, text: str, kb: list | None = None, edit: int | None = None):
        return await self.tg.message(chat, text, kb, edit)

    async def menu(self, chat: int, edit: int | None = None):
        return await self.send(chat,
            '<b>⛏ MinePulse — Minecraft Server Network</b>\n\n'
            'Каталог Java и Bedrock серверов, онлайн, избранное и уведомления.\n\n'
            'Для начала выбери действие 👇', MAIN_KB, edit)

    async def handle(self, update: dict) -> None:
        try:
            if 'pre_checkout_query' in update:
                await self.pre_checkout(update['pre_checkout_query'])
            elif 'callback_query' in update:
                await self.callback(update['callback_query'])
            elif 'message' in update:
                await self.message(update['message'])
        except Exception:
            LOG.exception('Error handling update %s', update.get('update_id'))
            # Avoid exposing internal errors, database paths or payment data to users.
            try:
                obj = update.get('message') or (update.get('callback_query') or {}).get('message')
                if obj and obj.get('chat', {}).get('type') == 'private':
                    await self.send(obj['chat']['id'], '⚠️ Не удалось выполнить запрос. Попробуй /start')
            except Exception:
                pass

    async def message(self, msg: dict) -> None:
        chat = msg.get('chat', {})
        if chat.get('type') != 'private':
            return
        uid = int(msg['from']['id'])
        self.db.register(uid, msg['from'].get('username') or '')
        if 'successful_payment' in msg:
            await self.payment(uid, msg['successful_payment'])
            return
        text = (msg.get('text') or '').strip()
        if not text:
            return
        command = text.split(maxsplit=1)[0].split('@', 1)[0].lower()
        arg = text.partition(' ')[2].strip()
        if command in ('/start', '/menu'):
            self.pending.pop(uid, None)
            await self.menu(uid)
        elif command == '/cancel':
            self.pending.pop(uid, None)
            await self.send(uid, 'Действие отменено.', MAIN_KB)
        elif command in ('/add',):
            await self.begin_add(uid)
        elif command in ('/catalog',):
            await self.catalog(uid, 'all', 'all', 0)
        elif command in ('/favorites',):
            await self.list_favorites(uid)
        elif command in ('/mine',):
            await self.list_mine(uid)
        elif command == '/stats':
            await self.stats(uid)
        elif command == '/help':
            await self.help(uid)
        elif command == '/terms':
            await self.terms(uid)
        elif command == '/privacy':
            await self.privacy(uid)
        elif command in ('/support', '/paysupport'):
            await self.support(uid)
        elif command == '/reply':
            await self.reply_to_support(uid, arg)
        elif command == '/refund':
            await self.admin_refund(uid, arg)
        elif command == '/search':
            if not arg:
                self.pending[uid] = Pending('search', time.monotonic())
                await self.send(uid, 'Отправь название или адрес сервера для поиска. /cancel — отмена.')
            else:
                await self.search(uid, arg)
        elif command == '/check':
            await self.quick_check(uid, arg)
        elif command == '/admin':
            await self.admin(uid)
        elif command == '/delete':
            await self.admin_delete(uid, arg)
        elif uid in self.pending:
            await self.on_pending(uid, text)
        else:
            await self.send(uid, 'Неизвестная команда. Используй меню 👇', MAIN_KB)

    async def callback(self, callback: dict) -> None:
        await self.tg.answer(callback['id'])
        message = callback.get('message') or {}
        if message.get('chat', {}).get('type') != 'private':
            return
        uid = user_id(callback)
        self.db.register(uid, callback['from'].get('username') or '')
        edit = message.get('message_id') if isinstance(message.get('message_id'), int) else None
        data = callback.get('data') or ''
        parts = data.split(':')
        action = parts[0]
        if action == 'menu':
            self.pending.pop(uid, None)
            await self.menu(uid, edit)
        elif action == 'catalog' and len(parts) == 4:
            edition, cat = parts[1:3]
            try:
                page = min(50, max(0, int(parts[3])))
            except ValueError:
                return
            await self.catalog(uid, edition, cat, page, edit)
        elif action == 'categories' and len(parts) == 2:
            await self.categories(uid, parts[1], edit)
        elif action == 'add':
            await self.begin_add(uid, edit)
        elif action == 'new' and len(parts) == 2 and parts[1] in ('java', 'bedrock'):
            self.pending[uid] = Pending('address', time.monotonic(), edition=parts[1])
            default = 25565 if parts[1] == 'java' else 19132
            await self.send(uid, f'Введи публичный адрес сервера (IP или домен).\n\n'
                                 f'Пример: <code>play.example.net:{default}</code>\n'
                                 'Если порт стандартный — его можно не писать.\n\n/cancel — отмена.')
        elif action == 'choose' and len(parts) == 2:
            pending = self.pending.get(uid)
            if pending and pending.stage == 'category' and parts[1] in CATEGORIES:
                pending.category = parts[1]
                pending.stage = 'name'
                await self.send(uid, 'Напиши <b>название сервера</b> (3–60 символов).\n/cancel — отмена.')
        elif action == 'favorites':
            await self.list_favorites(uid, edit)
        elif action == 'mine':
            await self.list_mine(uid, edit)
        elif action == 'stats':
            await self.stats(uid, edit)
        elif action == 'help':
            await self.help(uid, edit)
        elif action == 'agree' and len(parts) == 2:
            try:
                sid = int(parts[1])
            except ValueError:
                return
            server = self.db.server(sid)
            if server and server['added_by'] == uid:
                self.db.accept_terms(uid)
                await self.promote(uid, sid)
        elif action == 'find':
            self.pending[uid] = Pending('search', time.monotonic())
            await self.send(uid, 'Напиши название или IP сервера для поиска. /cancel — отмена.')
        elif action in ('server', 'fav', 'alert', 'refresh', 'promo') and len(parts) == 2:
            try:
                sid = int(parts[1])
            except ValueError:
                return
            if sid < 1:
                return
            if action == 'server':
                await self.card(uid, sid, edit)
            elif action == 'fav':
                self.db.favorite(uid, sid)
                await self.card(uid, sid, edit)
            elif action == 'alert':
                if self.db.toggle_alert(uid, sid) is None:
                    await self.send(uid, 'Сначала добавь сервер в избранное.')
                else:
                    await self.card(uid, sid, edit)
            elif action == 'refresh':
                key = (uid, sid)
                now = time.monotonic()
                if now - self.cooldowns.get(key, 0) < 30:
                    await self.send(uid, '⏳ Повторная проверка доступна через 30 секунд.')
                else:
                    self.cooldowns[key] = now
                    await self.check_one(sid)
                    await self.card(uid, sid, edit)
            elif action == 'promo':
                await self.promote(uid, sid)

    async def help(self, uid: int, edit: int | None = None):
        await self.send(uid,
           '<b>Команды MinePulse</b>\n'
           '/start — главное меню\n/catalog — каталог\n/add — добавить сервер\n'
           '/favorites — избранное\n/mine — мои публикации\n'
           '/search &lt;название&gt; — поиск\n'
           '/check java play.example.net — разовая проверка\n'
           '/check bedrock play.example.net:19132 — проверка Bedrock\n'
           '/stats — статистика\n/terms — условия\n/privacy — данные\n'
           '/paysupport — поддержка оплат\n/cancel — отмена ввода\n\n'
           '🔔 Уведомления нужно включить вручную в карточке избранного сервера.\n'
           '⭐ Платное продвижение доступно тому, кто добавил сервер.\n'
           'Проверка поддерживает стандартные протоколы Java и Bedrock. '
           'DNS SRV и часть нестандартных прокси пока не поддерживаются.\n\n'
           'MinePulse не является официальным продуктом Mojang или Microsoft.',
           [[btn('↩️ Меню', 'menu')]], edit)

    async def terms(self, uid: int, sid: int | None = None):
        text = (
            '<b>📜 Условия продвижения MinePulse</b>\n\n'
            f'• Цена: <b>{self.config.promo_stars} Telegram Stars</b>.\n'
            f'• Срок: <b>{self.config.promo_days} дней</b> с момента подтверждения оплаты.\n'
            '• Оплата поднимает публикацию выше бесплатных результатов, но не гарантирует переходов или игроков.\n'
            '• Сроки оплаченных продвижений одного сервера складываются.\n'
            '• Публикация может быть удалена за мошенничество, спам или нарушение правил Telegram.\n'
            '• По вопросам возврата и спорным оплатам используй /paysupport.\n'
            '• Покупая продвижение, ты подтверждаешь, что имеешь право размещать этот сервер.\n'
            '• Покупка цифровой услуги происходит через Telegram Stars.\n\n'
            'Поддержка покупок — администрация MinePulse, а не служба Telegram.'
        )
        kb = [[btn('✅ Согласен, перейти к оплате', f'agree:{sid}')]] if sid else []
        kb.append([btn('↩️ Меню', 'menu')])
        await self.send(uid, text, kb)

    async def privacy(self, uid: int):
        await self.send(uid,
            '<b>🔐 Данные MinePulse</b>\n\n'
            'Бот сохраняет Telegram ID, имя пользователя, добавленные серверы, избранное, настройки '
            'уведомлений и сведения об оплатах (ID транзакции, сумму и время).\n'
            'Токены Telegram и банковские карты пользователей не запрашиваются.\n'
            'Данные используются для работы бота и обработки платежей. '
            'Для обращения по поводу своих данных используй /support.',
            [[btn('↩️ Меню', 'menu')]])

    async def support(self, uid: int):
        if not self.config.admins:
            await self.send(uid, '⚠️ Администрация ещё не настроила поддержку. Покупки временно недоступны.')
            return
        self.pending[uid] = Pending('support', time.monotonic())
        await self.send(uid, '📨 Опиши вопрос одним сообщением (до 1000 символов).\n'
                        'При проблеме с платежом приложи дату и ID транзакции. /cancel — отмена.')

    async def reply_to_support(self, uid: int, arg: str):
        if uid not in self.config.admins:
            return
        parts = arg.split(maxsplit=1)
        if len(parts) != 2 or not parts[0].isdigit():
            await self.send(uid, 'Используй: /reply TELEGRAM_ID Ответ пользователю')
            return
        target = int(parts[0])
        if not self.db.one('SELECT id FROM users WHERE id=?', (target,)):
            await self.send(uid, 'Пользователь не найден.')
            return
        try:
            await self.send(target, '💬 <b>Ответ поддержки MinePulse:</b>\n' + bound(parts[1], 3000))
            await self.send(uid, '✅ Ответ отправлен.')
        except TelegramError:
            await self.send(uid, 'Не удалось доставить ответ.')

    async def admin_refund(self, uid: int, arg: str):
        if uid not in self.config.admins:
            return
        parts = arg.split(maxsplit=1)
        if len(parts) != 2 or not parts[0].isdigit() or len(parts[1]) > 256:
            await self.send(uid, 'Формат: /refund USER_ID TELEGRAM_PAYMENT_CHARGE_ID')
            return
        buyer, charge = int(parts[0]), parts[1].strip()
        if not self.db.lookup_payment(buyer, charge):
            await self.send(uid, 'Оплата не найдена или уже возвращена.')
            return
        try:
            await self.tg.call('refundStarPayment', user_id=buyer, telegram_payment_charge_id=charge)
        except TelegramError as exc:
            await self.send(uid, '❌ Telegram отклонил возврат: ' + bound(str(exc), 200))
            return
        self.db.mark_refunded(buyer, charge, self.config.promo_days)
        await self.send(uid, '✅ Возврат выполнен, срок продвижения скорректирован.')
        try:
            await self.send(buyer, '💫 Возврат оплаты MinePulse через Telegram Stars выполнен.')
        except TelegramError:
            pass

    async def begin_add(self, uid: int, edit: int | None = None):
        self.pending.pop(uid, None)
        await self.send(uid, '<b>Добавление Minecraft-сервера</b>\n\nВыбери редакцию:', EDITION_KB, edit)

    async def on_pending(self, uid: int, text: str) -> None:
        pending = self.pending[uid]
        if time.monotonic() - pending.created > 900:
            self.pending.pop(uid, None)
            await self.send(uid, 'Время ввода истекло. Начни заново: /add')
            return
        if pending.stage == 'search':
            self.pending.pop(uid, None)
            await self.search(uid, text)
        elif pending.stage == 'support':
            self.pending.pop(uid, None)
            if len(text) > 1000:
                await self.send(uid, 'Сообщение слишком длинное. Максимум 1000 символов. /paysupport')
                return
            for admin_id in self.config.admins:
                try:
                    await self.send(admin_id, f'📨 <b>Обращение в поддержку</b> от <code>{uid}</code>:\n'
                                    f'{bound(text, 1000)}\n\nОтветить: /reply {uid} Текст ответа')
                except TelegramError:
                    LOG.warning('Cannot deliver support request to admin %s', admin_id)
            await self.send(uid, '✅ Обращение отправлено администрации MinePulse.')
        elif pending.stage == 'address':
            try:
                endpoint = parse_endpoint(text, pending.edition)
            except AddressError as exc:
                await self.send(uid, '⚠️ ' + bound(str(exc), 250))
                return
            pending.address = endpoint.address
            pending.stage = 'category'
            await self.send(uid, f'Адрес: <code>{bound(endpoint.address,260)}</code>\n\nВыбери режим:', CAT_KB)
        elif pending.stage == 'name':
            if not 3 <= len(text) <= 60 or any(ord(c) < 32 for c in text):
                await self.send(uid, 'Название должно содержать 3–60 печатных символов.')
                return
            endpoint = parse_endpoint(pending.address, pending.edition)
            try:
                sid, created = self.db.add_server(endpoint, text, pending.category, uid,
                                                  self.config.max_servers_per_user)
            except ValueError as exc:
                await self.send(uid, '⚠️ ' + bound(str(exc), 200))
                self.pending.pop(uid, None)
                return
            self.pending.pop(uid, None)
            if created:
                await self.send(uid, '✅ Сервер добавлен! Проверяю подключение...')
                await self.check_one(sid)
            else:
                await self.send(uid, 'ℹ️ Этот адрес уже добавлен в каталог. Открываю карточку.')
            await self.card(uid, sid)

    async def categories(self, uid: int, edition: str, edit: int | None = None):
        if edition not in ('all', 'java', 'bedrock'):
            return
        kb = [[btn('📁 Все режимы', f'catalog:{edition}:all:0')]]
        kb += [[btn(value, f'catalog:{edition}:{key}:0')] for key, value in CATEGORIES.items()]
        kb += [[btn('↩️ В каталог', 'catalog:all:all:0')]]
        await self.send(uid, '<b>Выбери режим</b>', kb, edit)

    async def catalog(self, uid: int, edition: str, category: str, page: int,
                      edit: int | None = None):
        if edition not in ('all', 'java', 'bedrock') or category not in ('all', *CATEGORIES):
            return
        page = min(50, max(0, page))
        rows, more = self.db.catalog(edition, category, offset=page*6)
        header = f'<b>🧭 Каталог MinePulse</b>\nРедакция: {edition.upper()} | Режим: {bound(CATEGORIES.get(category,"Все"),30)}\n'
        kb = [[btn(short(server), f'server:{server["id"]}')] for server in rows]
        if not rows:
            header += '\nЗдесь пока нет серверов. Добавь первый!'
        else:
            header += '\n🟢 Онлайн   🔴 Оффлайн   ⚪ Нет данных\n⭐ Продвигаемые серверы отображаются выше.'
        paging = []
        if page > 0:
            paging.append(btn('◀️ Назад', f'catalog:{edition}:{category}:{page-1}'))
        if more:
            paging.append(btn('Далее ▶️', f'catalog:{edition}:{category}:{page+1}'))
        if paging:
            kb.append(paging)
        kb += [[btn('☕ Java', f'catalog:java:{category}:0'),
                btn('📱 Bedrock', f'catalog:bedrock:{category}:0'),
                btn('🌐 Все', f'catalog:all:{category}:0')],
               [btn('🎮 Режимы', f'categories:{edition}'), btn('🔍 Поиск', 'find')],
               [btn('➕ Добавить сервер', 'add'), btn('↩️ Меню', 'menu')]]
        await self.send(uid, header, kb, edit)

    async def search(self, uid: int, query: str):
        if len(query) > 75 or len(query) < 2:
            await self.send(uid, 'Поисковый запрос должен быть длиной 2–75 символов.')
            return
        rows, more = self.db.catalog(search=query, limit=12)
        kb = [[btn(short(s), f'server:{s["id"]}')] for s in rows]
        kb.append([btn('↩️ Каталог', 'catalog:all:all:0')])
        suffix = '\nПоказаны первые 12 результатов.' if more else ''
        await self.send(uid, f'🔎 Результаты: <b>{bound(query,75)}</b>\n'
                        + (f'Найдено: {len(rows)}{suffix}' if rows else 'Ничего не найдено.'), kb)

    async def card(self, uid: int, sid: int, edit: int | None = None):
        server = self.db.server(sid)
        if not server:
            await self.send(uid, 'Сервер не найден или удалён.', [[btn('↩️ Каталог','catalog:all:all:0')]], edit)
            return
        status = {'online': '🟢 Онлайн', 'offline': '🔴 Оффлайн', 'unknown': '⚪ Ещё проверяем'}[server['status']]
        timestamp = time.strftime('%d.%m.%Y %H:%M UTC', time.gmtime(server['last_checked_at'])) if server['last_checked_at'] else 'ещё не проверялся'
        fav = self.db.favorite_info(uid, sid)
        promoted = '⭐ <b>Продвигаемый сервер</b>\n' if server['promoted_until'] > time.time() else ''
        text = (
            f'<b>⛏ {bound(server["name"],60)}</b>\n{promoted}'
            f'<b>{status}</b> | {server["edition"].capitalize()}\n'
            f'📡 Адрес: <code>{bound(endpoint_for(server).address,260)}</code>\n'
            f'🎮 Режим: {bound(CATEGORIES.get(server["category"],"Другое"),40)}\n'
            f'🧩 Версия: {bound(server["version"] or "—",80)}\n'
            f'👥 Игроков: {server["players"]}/{server["max_players"]}\n'
            f'⚡ Пинг: {str(server["latency_ms"])+" мс" if server["status"]=="online" else "—"}\n'
            f'📝 MOTD: {bound(server["motd"] or "—",240)}\n'
            f'🕒 Проверка: {timestamp}\n'
            f'🆔 ID: <code>{sid}</code>'
        )
        kb = [[btn('💔 Удалить из избранного' if fav else '❤️ В избранное', f'fav:{sid}')]]
        if fav:
            kb.append([btn('🔕 Отключить уведомления' if fav['alerts'] else '🔔 Включить уведомления',
                           f'alert:{sid}')])
        kb.append([btn('🔄 Проверить сейчас', f'refresh:{sid}')])
        if server['added_by'] == uid:
            kb.append([btn(f'⭐ Продвижение: {self.config.promo_stars} Stars', f'promo:{sid}')])
        kb.append([btn('↩️ Каталог', 'catalog:all:all:0'), btn('🏠 Меню', 'menu')])
        await self.send(uid, text, kb, edit)

    async def list_favorites(self, uid: int, edit: int | None = None):
        rows = self.db.favorites(uid)
        kb = [[btn(short(s), f'server:{s["id"]}')] for s in rows]
        kb.append([btn('↩️ Меню', 'menu')])
        await self.send(uid, '<b>❤️ Избранные серверы</b>\n'
                        + ('Выбери сервер для просмотра.' if rows else 'Пока ничего нет. Добавляй серверы из каталога.'), kb, edit)

    async def list_mine(self, uid: int, edit: int | None = None):
        rows = self.db.mine(uid)
        kb = [[btn(short(s), f'server:{s["id"]}')] for s in rows]
        kb += [[btn('➕ Добавить', 'add'), btn('↩️ Меню', 'menu')]]
        await self.send(uid, f'<b>📡 Мои публикации</b> ({len(rows)}/{self.config.max_servers_per_user})\n'
                        + ('Нажми, чтобы открыть статистику и продвижение.' if rows else 'Ты ещё не добавил ни одного сервера.'), kb, edit)

    async def stats(self, uid: int, edit: int | None = None):
        s = self.db.statistics()
        await self.send(uid, '<b>📊 Статистика MinePulse</b>\n\n'
                        f'📡 Всего серверов: {s["total"]}\n'
                        f'🟢 Онлайн: {s["online"]}\n'
                        f'☕ Java: {s["java"]}\n'
                        f'📱 Bedrock: {s["bedrock"]}\n'
                        f'🧱 Добавлено тобой: {self.db.my_count(uid)}',
                        [[btn('↩️ Меню', 'menu')]], edit)

    async def quick_check(self, uid: int, arg: str):
        parts = arg.split()
        if len(parts) != 2 or parts[0].lower() not in ('java', 'bedrock'):
            await self.send(uid, 'Пример: <code>/check java play.example.net:25565</code>')
            return
        try:
            target = parse_endpoint(parts[1], parts[0].lower())
        except AddressError as exc:
            await self.send(uid, '⚠️ ' + bound(str(exc),200))
            return
        now = time.monotonic()
        if now-self.user_cooldowns.get(uid, 0) < 30:
            await self.send(uid, '⏳ Подожди 30 секунд перед следующей разовой проверкой.')
            return
        self.user_cooldowns[uid] = now
        try:
            async with self.semaphore:
                s = await asyncio.to_thread(check_server, target)
        except AddressError as exc:
            await self.send(uid, '⚠️ ' + bound(str(exc),200))
            return
        await self.send(uid, f'<b>{"🟢 Онлайн" if s.online else "🔴 Нет ответа"}</b>\n'
                        f'Адрес: <code>{bound(target.address,260)}</code>\n'
                        f'Версия: {bound(s.version or "—",80)}\n'
                        f'Игроков: {s.players}/{s.max_players}\n'
                        f'Пинг: {s.latency_ms if s.online else "—"} мс')

    async def check_one(self, sid: int) -> None:
        server = self.db.server(sid)
        if not server:
            return
        try:
            async with self.semaphore:
                result = await asyncio.to_thread(check_server, endpoint_for(server))
        except AddressError as exc:
            LOG.info('Cannot resolve server %d: %s', sid, exc)
            result = Status(False)
        except Exception:
            LOG.exception('Unexpected Minecraft probe error (server %d)', sid)
            result = Status(False)
        transition = self.db.update_status(sid, result)
        if transition:
            await self.notify(server, transition[1])

    async def notify(self, server: dict, status: str) -> None:
        icon = '🟢' if status == 'online' else '🔴'
        subscribers = self.db.subscribers(server['id'])
        for uid in subscribers:
            try:
                await self.send(uid, f'{icon} <b>{bound(server["name"],60)}</b> теперь '
                                     f'{"онлайн" if status=="online" else "оффлайн"}.\n'
                                     f'<code>{bound(endpoint_for(server).address,260)}</code>',
                                     [[btn('📡 Карточка сервера', f'server:{server["id"]}')]])
            except TelegramError as exc:
                LOG.warning('Could not alert %s: %s', uid, exc)
            await asyncio.sleep(0.05)

    async def monitor(self) -> None:
        while True:
            started = time.monotonic()
            servers = self.db.all_servers()
            if servers:
                await asyncio.gather(*(self.check_one(s['id']) for s in servers), return_exceptions=False)
            duration = time.monotonic() - started
            await asyncio.sleep(max(5, self.config.check_interval-duration))

    async def promote(self, uid: int, sid: int):
        server = self.db.server(sid)
        if not server or server['added_by'] != uid:
            await self.send(uid, 'Продвигать публикацию может только тот, кто её добавил.')
            return
        if not self.config.admins:
            await self.send(uid, '⚠️ Продвижение пока отключено: администратор не настроил поддержку.')
            return
        if not self.db.accepted_terms(uid):
            await self.terms(uid, sid)
            return
        payload = f'promo:{sid}:{uid}:{secrets.token_hex(12)}'
        if not self.db.create_invoice(payload, uid, sid, self.config.promo_stars):
            await self.send(uid, 'Не удалось создать счёт.')
            return
        await self.tg.call('sendInvoice', chat_id=uid,
                           title='⭐ Продвижение MinePulse',
                           description=f'Поднять сервер «{server["name"][:45]}» в каталоге на {self.config.promo_days} дней',
                           payload=payload, provider_token='', currency='XTR',
                           prices=[{'label': f'Продвижение на {self.config.promo_days} дней',
                                    'amount': self.config.promo_stars}])

    async def pre_checkout(self, checkout: dict):
        ok = self.db.invoice_valid(checkout.get('invoice_payload',''),
                                   int(checkout['from']['id']),
                                   int(checkout.get('total_amount',0)),
                                   checkout.get('currency',''))
        await self.tg.call('answerPreCheckoutQuery', pre_checkout_query_id=checkout['id'],
                           ok=ok, **({} if ok else {'error_message':'Счёт недействителен. Создай новый в боте.'}))

    async def payment(self, uid: int, payment: dict):
        result = self.db.pay(payment.get('invoice_payload',''), uid,
                             int(payment.get('total_amount',0)), payment.get('currency',''),
                             payment.get('telegram_payment_charge_id',''), self.config.promo_days)
        if result is None:
            LOG.error('Unrecognized Stars payment receipt for user %s', uid)
            await self.send(uid, '⚠️ Оплата получена, но активация не удалась. Свяжись с администратором бота.')
        elif result[1]:
            await self.send(uid, f'✅ Продвижение сервера #{result[0]} успешно включено на '
                                 f'{self.config.promo_days} дней!',
                            [[btn('Открыть сервер', f'server:{result[0]}')]])
        # Payment updates can be delivered more than once; avoid duplicate thank-you messages.

    async def admin(self, uid: int):
        if uid not in self.config.admins:
            return
        s = self.db.statistics()
        await self.send(uid, f'<b>🛠 Админ-панель</b>\nСерверов: {s["total"]}\n'
                        f'Онлайн: {s["online"]}\n'
                        'Удалить публикацию: /delete ID\n'
                        'Возврат: /refund USER_ID CHARGE_ID\n'
                        'Ответ: /reply USER_ID текст\n'
                        'Админ-ID указываются в .env → ADMIN_IDS.')

    async def admin_delete(self, uid: int, arg: str):
        if uid not in self.config.admins:
            return
        if not arg.isascii() or not arg.isdigit() or int(arg) < 1:
            await self.send(uid, 'Формат: /delete 123')
            return
        ok = self.db.delete(int(arg))
        await self.send(uid, '✅ Удалено.' if ok else 'Сервер не найден.')

    async def poll(self):
        await self.tg.call('deleteWebhook', drop_pending_updates=False)
        info = await self.tg.call('getMe')
        LOG.info('MinePulse started: @%s', info.get('username', 'unknown'))
        await self.tg.call('setMyCommands', commands=[
            {'command':'start','description':'Главное меню'},
            {'command':'catalog','description':'Каталог серверов'},
            {'command':'add','description':'Добавить сервер'},
            {'command':'favorites','description':'Избранное'},
            {'command':'mine','description':'Мои серверы'},
            {'command':'check','description':'Проверить сервер'},
            {'command':'help','description':'Помощь'},
            {'command':'terms','description':'Условия оплаты'},
            {'command':'paysupport','description':'Поддержка покупок'},
        ])
        offset = 0
        while True:
            try:
                updates = await self.tg.call('getUpdates', offset=offset, timeout=25,
                    limit=50, allowed_updates=['message', 'callback_query', 'pre_checkout_query'])
                for upd in updates:
                    offset = max(offset, int(upd['update_id'])+1)
                    await self.handle(upd)
            except TelegramError as exc:
                LOG.warning('Telegram polling error: %s', exc)
                await asyncio.sleep(3)
