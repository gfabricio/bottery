import asyncio
import logging

from aiohttp import web

from bottery import platform
from bottery.conf import settings
from bottery.exceptions import ImproperlyConfigured
from bottery.message import Message
from bottery.platform import discover_view
from bottery.user import User

logger = logging.getLogger('bottery.telegram')


def mixed_case(string):
    words = string.split('_')
    return words[0].lower() + ''.join([s.title() for s in words[1:]])


class TelegramAPI:
    api_url = 'https://api.telegram.org'
    methods = [
        'delete_webhook',
        'send_message',
        'set_webhook',
        'get_updates',
    ]

    def __init__(self, token, session):
        self.token = token
        self.session = session

    def make_url(self, method_name):
        method_name = mixed_case(method_name)
        return '{}/bot{}/{}'.format(self.api_url, self.token, method_name)

    def __getattr__(self, attr):
        if attr not in self.methods:
            raise AttributeError()

        url = self.make_url(attr)
        return lambda **kwargs: self.session.post(url, json=kwargs)


class TelegramUser(User):
    '''
    Telegram User reference
    https://core.telegram.org/bots/api#user
    '''
    def __init__(self, sender):
        self.id = sender['id']
        self.first_name = sender['first_name']
        self.last_name = sender.get('last_name')
        self.username = sender.get('username')
        self.language = sender.get('language_code')

    def __str__(self):
        s = '{u.first_name}'
        if self.last_name:
            s += ' {u.last_name}'

        s += ' ({u.id})'

        return s.format(u=self)


class TelegramEngine(platform.BaseEngine):
    platform = 'telegram'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api = TelegramAPI(self.token, session=self.session)

        # If no `mode` was defined at settings.py, use by default
        # polling mode. We need to test if `mode` is either `polling`
        # or `webhook`, if not, raise ImproperlyConfigured.
        if not hasattr(self, 'mode'):
            self.mode = 'polling'

    async def configure_polling(self):
        response = await self.api.delete_webhook()
        response = await response.json()
        if response['ok']:
            logger.debug('[%s] Polling mode set', self.engine_name)

        self.tasks.append(self.polling)

    async def polling(self, last_update=None):
        payload = {}
        if last_update:
            # `offset` param prevets from getting duplicates updates
            # from Telegram API:
            # https://core.telegram.org/bots/api#getupdates
            payload['offset'] = last_update + 1

        response = await self.api.get_updates(**payload)
        updates = await response.json()

        # If polling request returned at least one update, use its ID
        # to define the offset.
        if len(updates.get('result', [])):
            last_update = updates['result'][-1]['update_id']

        # Handle each new message, send its responses and then request
        # updates again.
        tasks = [self.message_handler(msg) for msg in updates['result']]
        await asyncio.gather(*tasks)
        asyncio.ensure_future(self.polling(last_update))

    async def configure_webhook(self):
        hostname = getattr(settings, 'HOSTNAME')
        if not hostname:
            raise ImproperlyConfigured('Missing HOSTNAME setting')

        response = await self.api.set_webhook(url=hostname)
        response = await response.json()
        if response['ok']:
            logger.debug('[%s] Webhook mode set', self.engine_name)

        self.server.router.add_post('/', self.webhook)

    async def webhook(self, request):
        update = await request.json()
        await asyncio.gather(self.message_handler(update))
        return web.Response()

    async def configure(self):
        method_name = 'configure_{}'.format(self.mode)
        configure_mode = getattr(self, method_name, None)
        if not configure_mode:
            msg = "There's no method to configure %s mode" % self.mode
            raise ImproperlyConfigured(msg)

        await configure_mode()

    def build_message(self, data):
        '''
        Return a Message instance according to the data received from
        Telegram API.
        https://core.telegram.org/bots/api#update
        '''
        message_data = data.get('message')

        if not message_data:
            return None

        return Message(
            id=message_data['message_id'],
            platform=self.platform,
            text=message_data['text'],
            user=TelegramUser(message_data['from']),
            timestamp=message_data['date'],
            raw=data,
        )

    async def message_handler(self, data):
        message = self.build_message(data)
        logger.info('[%s] Message from %s' % (self.engine_name, message.user))

        # Try to find a view (best name?) to response the message
        view = discover_view(message)
        if not view:
            return

        # TODO: Test if the view returned something or not
        response = await self.get_response(view, message)

        # TODO: Choose between Markdown and HTML
        # TODO: Verify response status
        await self.api.send_message(chat_id=message.user.id, text=response,
                                    parser_mode='markdown')


engine = TelegramEngine
