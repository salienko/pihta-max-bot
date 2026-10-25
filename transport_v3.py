"""MAX transport primitives used by the Telegram booking UI port."""
from types import SimpleNamespace
import re


def _cb(text, payload):
    return {'type': 'callback', 'text': str(text)[:128], 'payload': str(payload)[:1024]}


def _msg(text):
    return {'type': 'message', 'text': str(text)[:128]}


class InlineKeyboardButton:
    def __init__(self, text, callback_data=None, url=None):
        self.text, self.callback_data, self.url = text, callback_data, url


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard=None):
        self.inline_keyboard = inline_keyboard or []

    def to_max(self):
        rows = []
        for row in self.inline_keyboard:
            out = []
            for b in row:
                out.append({'type': 'link', 'text': b.text[:128], 'url': b.url[:2048]} if b.url else _cb(b.text, b.callback_data or ''))
            rows.append(out)
        return {'type': 'inline_keyboard', 'payload': {'buttons': rows}}


class KeyboardButton:
    def __init__(self, text, request_contact=False):
        self.text, self.request_contact = text, request_contact


class ReplyKeyboardMarkup:
    def __init__(self, keyboard=None, resize_keyboard=True, one_time_keyboard=False):
        self.keyboard = keyboard or []

    def to_max(self):
        rows = []
        for row in self.keyboard:
            rows.append([{'type': 'request_contact', 'text': b.text[:128]} if b.request_contact else _msg(b.text) for b in row])
        return {'type': 'inline_keyboard', 'payload': {'buttons': rows}}


class ReplyKeyboardRemove:
    def to_max(self):
        return None


def markup_attachments(markup):
    if not markup:
        return None
    if hasattr(markup, 'to_max'):
        value = markup.to_max()
        return [value] if value else []
    return None


TEL_RE = re.compile(r'TEL[^:\n]*:([+\d()\s\-]+)')


class MaxMessage:
    def __init__(self, api, raw, banner_cache=None, reply_user_id=None):
        self.api, self.raw = api, raw
        body = raw.get('body') or {}
        self.message_id = str(body.get('mid') or '')
        self.text = body.get('text') or ''
        sender = raw.get('sender') or {}
        recipient = raw.get('recipient') or {}
        self.from_user = SimpleNamespace(id=sender.get('user_id'))
        self.chat = SimpleNamespace(type='private' if recipient.get('chat_type') == 'dialog' else recipient.get('chat_type', 'chat'))
        self.recipient_user_id = reply_user_id if reply_user_id is not None else sender.get('user_id') if recipient.get('chat_type') == 'dialog' else recipient.get('user_id')
        self.chat_id = recipient.get('chat_id') if recipient.get('chat_type') != 'dialog' else None
        self.raw_body = body
        self.photo = next((a for a in body.get('attachments') or [] if a.get('type') == 'image'), None)
        self.contact = None
        for a in body.get('attachments') or []:
            if a.get('type') == 'contact':
                p = a.get('payload') or {}
                m = TEL_RE.search(p.get('vcf_info') or '')
                self.contact = SimpleNamespace(phone_number=m.group(1).strip() if m else None, user_id=(p.get('max_info') or {}).get('user_id'))
        self._banner_cache = banner_cache

    async def answer(self, text, reply_markup=None):
        return await self.api.send(user_id=self.recipient_user_id, chat_id=self.chat_id, text=text, attachments=markup_attachments(reply_markup))

    async def answer_photo(self, photo, caption, reply_markup=None):
        attachments = []
        if isinstance(photo, dict):
            attachments.append(photo)
        elif self._banner_cache and self._banner_cache.get('token'):
            attachments.append({'type': 'image', 'payload': {'token': self._banner_cache['token']}})
        attachments += markup_attachments(reply_markup) or []
        return await self.api.send(user_id=self.recipient_user_id, chat_id=self.chat_id, text=caption, attachments=attachments or None)

    async def edit_text(self, text, reply_markup=None):
        return await self.api.edit(self.message_id, text=text, attachments=markup_attachments(reply_markup))

    async def edit_caption(self, caption, reply_markup=None):
        return await self.edit_text(caption, reply_markup)

    async def edit_media(self, media, reply_markup=None):
        return await self.edit_text(getattr(media, 'caption', '') or '', reply_markup)

    async def edit_reply_markup(self, reply_markup=None):
        return await self.edit_text(self.text, reply_markup)


class MaxCallback:
    def __init__(self, api, update, banner_cache=None):
        self.api = api
        cb = update.get('callback') or {}
        self.callback_id = cb.get('callback_id')
        self.data = cb.get('payload') or ''
        user = cb.get('user') or {}
        self.from_user = SimpleNamespace(id=user.get('user_id'))
        self.message = MaxMessage(api, update.get('message') or {}, banner_cache, reply_user_id=user.get('user_id')) if update.get('message') else None

    async def answer(self, text=None, show_alert=False):
        if self.callback_id:
            try: return await self.api.answer(self.callback_id, notification=text)
            except Exception: return None


class TelegramBadRequest(Exception):
    pass


class FSInputFile:
    def __init__(self, path): self.path = str(path)


class InputMediaPhoto:
    def __init__(self, media=None, caption=None): self.media, self.caption = media, caption


class Router:
    def __init__(self):
        self.message, self.callback_query = SimpleNamespace(register=lambda *a, **k: None), SimpleNamespace(register=lambda *a, **k: None)
