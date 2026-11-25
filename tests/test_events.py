import json
import unittest

from lib import events


class Socket:
    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []

    def send(self, message):
        if self.fail:
            raise BrokenPipeError('Disconnected test phone')
        self.messages.append(json.loads(message))


class BrowserEventTests(unittest.TestCase):
    def tearDown(self):
        for client_id in list(events.ip2websock):
            events.unregister_client(client_id)

    def test_one_disconnected_phone_cannot_interrupt_other_phones(self):
        broken_id = events.register_client(Socket(fail=True), 'phone1')
        good = Socket()
        events.register_client(good, 'phone2')
        payload = {'type': 'queue', 'data': [{'title': 'Don\'t Stop "唱歌"', 'user': '<Singer>'}]}
        events.broadcast(payload)
        self.assertEqual(good.messages, [payload])
        self.assertNotIn(broken_id, events.ip2websock)

    def test_two_tabs_on_one_phone_have_independent_lifetimes(self):
        first, second = Socket(), Socket()
        first_id = events.register_client(first, 'same-ip')
        second_id = events.register_client(second, 'same-ip')
        self.assertNotEqual(first_id, second_id)
        events.unregister_client(first_id)
        events.broadcast({'type': 'queue', 'data': []})
        self.assertEqual(len(second.messages), 1)
        self.assertIn(second_id, events.ip2websock)

    def test_notifications_are_data_and_only_reach_the_target_phone(self):
        first, second = Socket(), Socket()
        events.register_client(first, 'first')
        events.register_client(second, 'second')
        events.flash('唱歌 "hello" Don\'t', 'is-success', client_ip='first')
        self.assertEqual(first.messages[0]['type'], 'notification')
        self.assertEqual(first.messages[0]['data']['message'], '唱歌 "hello" Don\'t')
        self.assertEqual(second.messages, [])

    def test_registering_during_broadcast_does_not_mutate_the_iteration(self):
        added = Socket()
        class RegisteringSocket(Socket):
            def send(self, message):
                super().send(message)
                events.register_client(added, 'new-phone')
        events.register_client(RegisteringSocket(), 'original')
        events.broadcast({'type': 'queue', 'data': []})
        events.broadcast({'type': 'queue', 'data': []})
        self.assertEqual(len(added.messages), 1)


if __name__ == '__main__':
    unittest.main()
