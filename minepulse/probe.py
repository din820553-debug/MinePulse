"""Minecraft status: Java 1.7+ and legacy fallback, Bedrock RakNet pong.

All destination socket addresses are resolved, checked and pinned before use;
no requests are made to user-provided private, loopback or reserved IPs.
"""
from __future__ import annotations
from dataclasses import dataclass
import ipaddress
import json
import re
import secrets
import socket
import struct
import time
from typing import Any


MOTD_STYLES = re.compile(r'§[0-9a-fk-or]', re.IGNORECASE)
LABEL = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')
MAGIC = bytes.fromhex('00ffff00fefefefefdfdfdfd12345678')
MAX_PACKET = 1_000_000


class AddressError(ValueError):
    pass


@dataclass(frozen=True)
class Endpoint:
    edition: str
    host: str
    port: int

    @property
    def address(self) -> str:
        host = f'[{self.host}]' if ':' in self.host else self.host
        return f'{host}:{self.port}'


@dataclass(frozen=True)
class Status:
    online: bool
    version: str = ''
    players: int = 0
    max_players: int = 0
    latency_ms: int = 0
    motd: str = ''


def parse_endpoint(text: str, edition: str) -> Endpoint:
    if edition not in ('java', 'bedrock'):
        raise AddressError('Выбери Java или Bedrock Edition.')
    address = text.strip().rstrip('.')
    if not address or len(address) > 260 or any(c.isspace() for c in address):
        raise AddressError('Введи адрес вида play.example.net:25565')
    if any(x in address for x in ('/', '@', '#', '?', '\\', '%')):
        raise AddressError('Нужен адрес сервера, а не URL.')
    default = 25565 if edition == 'java' else 19132
    if address.startswith('['):
        match = re.fullmatch(r'\[([^\[\]]+)\](?::(\d{1,5}))?', address)
        if not match:
            raise AddressError('IPv6 указывай как [2001:db8::1]:25565')
        host, raw_port = match.groups()
    else:
        if address.count(':') > 1:
            raise AddressError('IPv6 указывай в квадратных скобках.')
        parts = address.rsplit(':', 1)
        host, raw_port = (parts[0], parts[1]) if len(parts) == 2 else (address, None)
    if raw_port is not None and not raw_port.isascii():
        raise AddressError('Порт должен быть числом.')
    try:
        port = int(raw_port) if raw_port is not None else default
    except ValueError as exc:
        raise AddressError('Порт должен быть числом.') from exc
    if port < 1 or port > 65535:
        raise AddressError('Порт должен быть от 1 до 65535.')
    host = host.lower().rstrip('.')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError as exc:
            raise AddressError('Недопустимое имя сервера.') from exc
        if len(host) > 253 or '.' not in host or not all(LABEL.fullmatch(part) for part in host.split('.')):
            raise AddressError('Используй публичный домен или IP-адрес сервера.')
        if host.endswith(('.local', '.localhost', '.internal', '.test', '.invalid', '.example')):
            raise AddressError('Локальные и тестовые домены не поддерживаются.')
    else:
        if not ip.is_global:
            raise AddressError('Частные, локальные и служебные IP-адреса запрещены.')
        host = ip.compressed
    return Endpoint(edition=edition, host=host, port=port)


def resolve_public(endpoint: Endpoint) -> list[tuple[int, int, int, tuple[Any, ...]]]:
    sock_type = socket.SOCK_STREAM if endpoint.edition == 'java' else socket.SOCK_DGRAM
    try:
        records = socket.getaddrinfo(endpoint.host, endpoint.port, type=sock_type)
    except OSError as exc:
        raise AddressError('Домен не удалось разрешить. Проверь адрес и порт.') from exc
    if not records:
        raise AddressError('Нет DNS-записей сервера.')
    found = []
    for family, kind, proto, _, sockaddr in records:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:
            raise AddressError('DNS ведёт на локальный или служебный IP-адрес.')
        found.append((family, kind, proto, sockaddr))
    if not found:
        raise AddressError('Не найден публичный IP-адрес сервера.')
    return found[:8]


def varint(number: int) -> bytes:
    out = bytearray()
    number &= 0xffffffff
    while True:
        part = number & 0x7f
        number >>= 7
        out.append(part | (0x80 if number else 0))
        if not number:
            return bytes(out)


def read_exact(sock: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        block = sock.recv(count - len(data))
        if not block:
            raise OSError('Соединение закрыто сервером.')
        data.extend(block)
    return bytes(data)


def read_varint(sock: socket.socket) -> int:
    value = 0
    for i in range(5):
        b = read_exact(sock, 1)[0]
        value |= (b & 0x7f) << (7 * i)
        if not b & 0x80:
            return value
    raise ValueError('Слишком длинный VarInt.')


def clean_motd(raw: Any) -> str:
    if isinstance(raw, dict):
        text = str(raw.get('text', ''))
        for extra in raw.get('extra', []):
            text += clean_motd(extra)
    elif isinstance(raw, list):
        text = ''.join(clean_motd(item) for item in raw)
    else:
        text = str(raw or '')
    text = MOTD_STYLES.sub('', text)
    return ''.join(c for c in text if c.isprintable() or c == '\n').strip()[:240]


def _probe_java(endpoint: Endpoint, record: tuple, timeout: float) -> Status:
    family, socktype, proto, sockaddr = record
    with socket.socket(family, socktype, proto) as s:
        s.settimeout(timeout)
        start = time.monotonic()
        s.connect(sockaddr)
        host = endpoint.host.encode('utf-8')
        handshake = b'\x00' + varint(47) + varint(len(host)) + host + struct.pack('>H', endpoint.port) + b'\x01'
        s.sendall(varint(len(handshake)) + handshake + b'\x01\x00')
        packet_size = read_varint(s)
        if packet_size < 2 or packet_size > MAX_PACKET:
            raise ValueError('Недопустимый размер ответа Java.')
        # Read packet in one bounded operation; packet ID 0 and JSON length prefix.
        packet = read_exact(s, packet_size)
        pos = 0
        def consume_varint() -> int:
            nonlocal pos
            number = 0
            for i in range(5):
                if pos >= len(packet):
                    raise ValueError('Обрезанный пакет.')
                byte = packet[pos]
                pos += 1
                number |= (byte & 0x7f) << (7*i)
                if not byte & 0x80:
                    return number
            raise ValueError('Некорректный VarInt.')
        if consume_varint() != 0:
            raise ValueError('Не статус-пакет.')
        json_len = consume_varint()
        if json_len < 1 or json_len != len(packet) - pos:
            raise ValueError('Неверная длина JSON.')
        data = json.loads(packet[pos:].decode('utf-8'))
        players = data.get('players') or {}
        version = data.get('version') or {}
        return Status(True, str(version.get('name', 'Неизвестно'))[:80],
                      int(players.get('online', 0)), int(players.get('max', 0)),
                      int((time.monotonic() - start) * 1000), clean_motd(data.get('description')))


def _probe_legacy(endpoint: Endpoint, record: tuple, timeout: float) -> Status:
    family, socktype, proto, sockaddr = record
    with socket.socket(family, socktype, proto) as s:
        s.settimeout(timeout)
        start = time.monotonic()
        s.connect(sockaddr)
        s.sendall(b'\xfe\x01')
        if read_exact(s, 1) != b'\xff':
            raise ValueError('Не legacy-ответ.')
        size = struct.unpack('>H', read_exact(s, 2))[0]
        if size > 5000:
            raise ValueError('Длинный legacy-ответ.')
        content = read_exact(s, size * 2).decode('utf-16-be')
        if content.startswith('§1\x00'):
            parts = content.split('\x00')
            if len(parts) < 6:
                raise ValueError('Некорректный legacy-ответ.')
            _, _, version, motd, online, maximum = parts[:6]
        else:
            bits = content.rsplit('§', 2)
            if len(bits) != 3:
                raise ValueError('Некорректный legacy-ответ.')
            motd, online, maximum = bits
            version = 'Legacy (до 1.7)'
        return Status(True, version[:80], int(online), int(maximum),
                      int((time.monotonic() - start) * 1000), clean_motd(motd))


def _probe_bedrock(endpoint: Endpoint, record: tuple, timeout: float) -> Status:
    family, socktype, proto, sockaddr = record
    with socket.socket(family, socktype, proto) as s:
        s.settimeout(timeout)
        s.connect(sockaddr)
        started = time.monotonic()
        packet = b'\x01' + struct.pack('>Q', int(time.time() * 1000)) + MAGIC + secrets.token_bytes(8)
        s.send(packet)
        response = s.recv(4096)
        if len(response) < 35 or response[0] != 0x1c or response[17:33] != MAGIC:
            raise ValueError('Некорректный RakNet-ответ.')
        length = struct.unpack('>H', response[33:35])[0]
        if length == 0 or 35 + length > len(response):
            raise ValueError('Неверная длина Bedrock MOTD.')
        fields = response[35:35+length].decode('utf-8', 'replace').split(';')
        if len(fields) < 6 or fields[0] not in ('MCPE', 'MCEE'):
            raise ValueError('Некорректные сведения Bedrock.')
        return Status(True, fields[3][:80], int(fields[4]), int(fields[5]),
                      int((time.monotonic()-started)*1000), clean_motd(fields[1]))


def check_server(endpoint: Endpoint, timeout: float = 3.0) -> Status:
    """Synchronous IO, safe to invoke through asyncio.to_thread().

    Returns offline instead of throwing on normal network/protocol failures.
    AddressError denotes a forbidden or unresolvable network destination.
    """
    records = resolve_public(endpoint)  # Never connect to a user-supplied raw hostname.
    for record in records:
        try:
            if endpoint.edition == 'bedrock':
                return _probe_bedrock(endpoint, record, timeout)
            try:
                return _probe_java(endpoint, record, timeout)
            except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
                return _probe_legacy(endpoint, record, timeout)
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            continue
    return Status(False)
