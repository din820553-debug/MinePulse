import time
import unittest
from minepulse.database import Database
from minepulse.probe import Endpoint, Status


class DBTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.db.register(100,'alice')
        self.db.register(200,'bob')
        self.endpoint = Endpoint('java','mc.example.org',25565)
        self.sid, created = self.db.add_server(self.endpoint,'My Server','survival',100,5)
        self.assertTrue(created)

    def tearDown(self):
        self.db.close()

    def test_duplicate_and_catalog(self):
        other_id, created = self.db.add_server(self.endpoint,'Fake','other',200,5)
        self.assertFalse(created)
        self.assertEqual(other_id,self.sid)
        self.assertEqual(self.db.catalog('java','survival')[0][0]['name'],'My Server')
        self.assertFalse(self.db.catalog('bedrock')[0])
        self.assertFalse(self.db.catalog(search='a%notfound')[0])

    def test_subscriptions_offline_requires_two_failures(self):
        self.assertTrue(self.db.favorite(200,self.sid))
        self.assertEqual(self.db.subscribers(self.sid), [])
        self.assertTrue(self.db.toggle_alert(200,self.sid))
        self.assertEqual(self.db.subscribers(self.sid), [200])
        self.assertIsNone(self.db.update_status(self.sid, Status(True,'1.21',3,10,20,'hello')))
        self.assertIsNone(self.db.update_status(self.sid, Status(False)))
        self.assertEqual(self.db.server(self.sid)['status'],'online')
        self.assertEqual(self.db.update_status(self.sid, Status(False)), ('online','offline'))
        self.assertEqual(self.db.update_status(self.sid, Status(True)), ('offline','online'))
        self.assertFalse(self.db.favorite(200,self.sid))
        self.assertEqual(self.db.subscribers(self.sid), [])

    def test_payment_idempotent_and_safe_delete(self):
        self.assertFalse(self.db.create_invoice('bad',200,self.sid,50))
        self.assertTrue(self.db.create_invoice('promo-test',100,self.sid,50))
        self.assertFalse(self.db.invoice_valid('promo-test',100,50,'XTR'))
        self.db.accept_terms(100)
        self.assertTrue(self.db.invoice_valid('promo-test',100,50,'XTR'))
        self.assertFalse(self.db.invoice_valid('promo-test',100,51,'XTR'))
        self.assertEqual(self.db.pay('promo-test',100,50,'XTR','unique-charge',7), (self.sid,True))
        first = self.db.server(self.sid)['promoted_until']
        self.assertGreater(first,int(time.time()))
        self.assertEqual(self.db.pay('promo-test',100,50,'XTR','unique-charge',7), (self.sid,False))
        self.assertEqual(self.db.server(self.sid)['promoted_until'],first)
        self.assertIsNone(self.db.pay('promo-test',100,50,'XTR','other-charge',7))
        self.assertTrue(self.db.delete(self.sid))
        self.assertFalse(self.db.server(self.sid))
        self.assertIsNone(self.db.pay('promo-test',100,50,'XTR','other-charge',7))

    def test_refund(self):
        self.db.accept_terms(100)
        self.assertTrue(self.db.create_invoice('refund-test',100,self.sid,50))
        self.db.pay('refund-test',100,50,'XTR','charge-99',7)
        self.assertTrue(self.db.lookup_payment(100,'charge-99'))
        self.assertTrue(self.db.mark_refunded(100,'charge-99',7))
        self.assertIsNone(self.db.lookup_payment(100,'charge-99'))
        self.assertFalse(self.db.mark_refunded(100,'charge-99',7))
