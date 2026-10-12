import ipaddress
import json
import socket
import struct
import threading
import unittest
from unittest.mock import patch

from minepulse.probe import (
    AddressError, Endpoint, MAGIC, check_server, clean_motd,
    parse_endpoint, resolve_public, varint,
)


class EndpointTests(unittest.TestCase):
    def test_valid_domains_and_ports(self):
        self.assertEqual(parse_endpoint('Play.Example.org', 'java').address,
                         'play.example.org:25565')
        self.assertEqual(parse_endpoint('mc.example.org:19140', 'bedrock').port, 19140)
        self.assertEqual(parse_endpoint('1.1.1.1', 'java').port, 25565)
        self.assertEqual(parse_endpoint('[2606:4700:4700::1111]:25565', 'java').port, 25565)

    def test_bad_addresses(self):
        for bad in ('127.0.0.1', '192.168.2.1', '10.0.0.1', '169.254.1.1',
                    'localhost', 'host.local', 'mc.example.org:0',
                    'mc.example.org:70000', 'https://mc.example.org',
                    'mc.example.org/secret', 'user@mc.example.org',
                    '[::1]:25565', 'mc.example.org:abc', 'x..com'):
            with self.subTest(bad=bad), self.assertRaises(AddressError):
                parse_endpoint(bad, 'java')

    def test_dns_private_rejected(self):
        target = parse_endpoint('mc.example.org', 'java')
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 25565))]
        with patch('socket.getaddrinfo', return_value=private):
            with self.assertRaises(AddressError):
                resolve_public(target)

    def test_motd_clean(self):
        self.assertEqual(clean_motd({'text':'§aHi ', 'extra':[{'text':'§bthere'}]}), 'Hi there')

    def test_varint(self):
        self.assertEqual(varint(0), b'\x00')
        self.assertEqual(varint(300), b'\xac\x02')


class ProtocolTests(unittest.TestCase):
    def test_java_tcp_status_protocol(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        data = {'description': {'text':'§aHello'}, 'players': {'online': 12, 'max': 99},
                'version': {'name': '1.21.8'}}
        content = json.dumps(data).encode()
        packet = b'\x00' + varint(len(content)) + content

        def accept_and_serve():
            with listener:
                c, _ = listener.accept()
                with c:
                    c.settimeout(3)
                    c.recv(1024)
                    c.sendall(varint(len(packet)) + packet)
        thread = threading.Thread(target=accept_and_serve, daemon=True)
        thread.start()
        with patch('minepulse.probe.resolve_public', return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, ('127.0.0.1',port))]):
            s = check_server(Endpoint('java','mc.example.org',port))
        thread.join(3)
        self.assertTrue(s.online)
        self.assertEqual(s.players, 12)
        self.assertEqual(s.version, '1.21.8')
        self.assertEqual(s.motd, 'Hello')

    def test_legacy_java_status(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        port = listener.getsockname()[1]
        content = '\u00a71\x00127\x001.6.4\x00Legacy Mine\x005\x00120'
        encoded = content.encode('utf-16-be')
        def accept_and_serve():
            with listener:
                for i in range(2):
                    c, _ = listener.accept()
                    with c:
                        c.settimeout(3)
                        c.recv(1024)
                        if i == 1:
                            c.sendall(b'\xff' + struct.pack('>H',len(content)) + encoded)
        thread = threading.Thread(target=accept_and_serve, daemon=True)
        thread.start()
        with patch('minepulse.probe.resolve_public', return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, ('127.0.0.1',port))]):
            s = check_server(Endpoint('java','mc.example.org',port))
        thread.join(3)
        self.assertTrue(s.online)
        self.assertEqual(s.version, '1.6.4')
        self.assertEqual(s.players, 5)

    def test_bedrock_udp_status_protocol(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        content = b'MCPE;Bedrock Test;766;1.21.40;15;100;1234;World;Survival;1;19132;19133;'
        pong = b'\x1c' + b'\x00'*16 + MAGIC + struct.pack('>H', len(content)) + content
        def receive_and_reply():
            with listener:
                listener.settimeout(3)
                req, addr = listener.recvfrom(1024)
                self.assertEqual(req[0], 1)
                listener.sendto(pong, addr)
        thread = threading.Thread(target=receive_and_reply, daemon=True)
        thread.start()
        with patch('minepulse.probe.resolve_public', return_value=[
            (socket.AF_INET, socket.SOCK_DGRAM, 17, ('127.0.0.1',port))]):
            s = check_server(Endpoint('bedrock','mc.example.org',port))
        thread.join(3)
        self.assertTrue(s.online)
        self.assertEqual((s.version,s.players,s.max_players), ('1.21.40',15,100))
