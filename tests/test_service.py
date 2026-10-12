import unittest
from unittest.mock import patch

from minepulse.config import Config
from minepulse.database import Database
from minepulse.probe import Status
from minepulse.service import MinePulse


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.called = []
    async def message(self, chat_id, text, keyboard=None, edit_id=None):
        self.sent.append((chat_id,text,keyboard,edit_id))
    async def answer(self, callback_id, text=''):
        pass
    async def call(self, method, **params):
        self.called.append((method,params))


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(':memory:')
        self.tg = FakeTelegram()
        self.app = MinePulse(Config('demo:token', db_path=':memory:',
                                   admins=frozenset({999})), self.db, self.tg)
        await self.app.message({'chat':{'type':'private','id':123},
                                'from':{'id':123,'username':'test'},'text':'/start'})

    async def asyncTearDown(self):
        self.db.close()

    async def callback(self, data, uid=123):
        await self.app.callback({'id':'cb','data':data,
             'from':{'id':uid,'username':'test'},
             'message':{'message_id':1,'chat':{'id':uid,'type':'private'}}})

    async def text(self, text, uid=123):
        await self.app.message({'chat':{'type':'private','id':uid},
                                'from':{'id':uid},'text':text})

    async def test_full_add_flow_and_star_promotion(self):
        await self.callback('add')
        await self.callback('new:bedrock')
        await self.text('bedrock.example.org:19133')
        await self.callback('choose:survival')
        with patch('minepulse.service.check_server', return_value=Status(True,'1.21',12,50,15,'Hi')):
            await self.text('Example Bedrock')
        listing = self.db.server(1)
        self.assertEqual(listing['status'],'online')
        self.assertEqual(listing['edition'],'bedrock')
        await self.callback('fav:1')
        await self.callback('alert:1')
        self.assertEqual(self.db.subscribers(1),[123])
        await self.callback('promo:1')
        self.assertFalse(any(method == 'sendInvoice' for method, _ in self.tg.called))
        self.assertTrue(any('Условия продвижения' in row[1] for row in self.tg.sent))
        await self.callback('agree:1')
        invoices = [(m,p) for m,p in self.tg.called if m == 'sendInvoice']
        self.assertEqual(len(invoices),1)
        payload = invoices[0][1]['payload']
        self.assertEqual(invoices[0][1]['currency'],'XTR')
        await self.app.pre_checkout({'id':'pc1','from':{'id':123},'invoice_payload':payload,
                                     'total_amount':50,'currency':'XTR'})
        self.assertTrue(self.tg.called[-1][1]['ok'])
        payment={'invoice_payload':payload,'total_amount':50,'currency':'XTR',
                 'telegram_payment_charge_id':'unique-123'}
        await self.app.payment(123,payment)
        self.assertGreater(self.db.server(1)['promoted_until'],0)
        first = self.db.server(1)['promoted_until']
        await self.app.payment(123,payment)
        self.assertEqual(self.db.server(1)['promoted_until'],first)

    async def test_alert_transitions_and_search(self):
        await self.callback('add')
        await self.callback('new:java')
        await self.text('java.example.org')
        await self.callback('choose:other')
        with patch('minepulse.service.check_server', return_value=Status(True,'1.20',1,2)):
            await self.text('Test Java')
        await self.callback('fav:1')
        await self.callback('alert:1')
        with patch('minepulse.service.check_server', return_value=Status(False)):
            await self.app.check_one(1)
            self.assertEqual(self.db.server(1)['status'],'online')
            await self.app.check_one(1)
        self.assertEqual(self.db.server(1)['status'],'offline')
        self.assertTrue(any('оффлайн' in msg[1] for msg in self.tg.sent))
        await self.text('/search Java')
        self.assertTrue(any('Test Java' in str(msg[2]) for msg in self.tg.sent))

    async def test_admin_permission(self):
        await self.text('/delete 1')
        self.assertFalse(any('Удалено' in msg[1] for msg in self.tg.sent))

    async def test_support_request_and_admin_reply(self):
        await self.text('/paysupport')
        await self.text('Проблема с оплатой')
        self.assertTrue(any(x[0] == 999 and 'Проблема с оплатой' in x[1] for x in self.tg.sent))
        await self.text('/reply 123 Ответ готов',999)
        self.assertTrue(any(x[0] == 123 and 'Ответ готов' in x[1] for x in self.tg.sent))
