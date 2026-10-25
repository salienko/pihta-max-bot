"""Aiogram-like transport shim over the MAX Bot API.

Mirrors the small surface of aiogram types that bot.py / checkout.py use:
Message.answer / answer_photo / edit_text / edit_caption / edit_reply_markup,
InlineKeyboardMarkup, ReplyKeyboardMarkup (as MAX `message`-buttons),
CallbackQuery.answer. Lets the booking logic stay untouched.
"""
import asyncio
import re
from types import SimpleNamespace


# ---------- keyboards ----------

def _callback_button(text, payload):
    return {'type': 'callback', 'text': text[:128], 'payload': payload[:1024]}


class InlineKeyboardButton:
    def __init__(self, text, callback_data=None):
        self.text = text
        self.callback_data = callback_data


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard=None):
        self.inline_keyboard = inline_keyboard or []

    def to_max(self):
        rows = []
        for row in self.inline_keyboard:
            rows.append([_callback_button(b.text, b.callback_data) for b in row])
        return {'type': 'inline_keyboard', 'payload': {'buttons': rows}}

    def __bool__(self):
        return bool(self.inline_keyboard)


class KeyboardButton:
    def __init__(self, text, request_contact=False):
        self.text = text
        self.request_contact = request_contact


class ReplyKeyboardMarkup:
    """Emulated with MAX inline keyboard: plain buttons send their text as a
    user message; request_contact buttons ask for the user's contact."""

    def __init__(self, keyboard=None, resize_keyboard=True, one_time_keyboard=False):
        self.keyboard = keyboard or []

    def to_max(self):
        rows = []
        for row in self.keyboard:
            buttons = []
            for b in row:
                if b.request_contact:
                    buttons.append({'type': 'request_contact', 'text': b.text[:128]})
                else:
                    buttons.append({'type': 'message', 'text': b.text[:128]})
            rows.append(buttons)
        return {'type': 'inline_keyboard', 'payload': {'buttons': rows}}


def _attachment(markup):
    if markup is None:
        return None
    if isinstance(markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup)):
        return [markup.to_max()] if markup else None
    return None


class MaxApiError(Exception):
    pass


# ---------- chat / user ----------

class _Chat:
    def __init__(self, chat_type):
        self.type = 'private' if chat_type == 'dialog' else chat_type


class _User:
    def __init__(self, user_id):
        self.id = user_id


# ---------- messages ----------

TEL_RE = re.compile(r'TEL[^:\n]*:([+\d()\s\-]+)')


class MaxMessage:
    """Wraps an incoming MAX message; sends/edits through the MaxApi client."""

    def __init__(self, api, raw_message, *, banner_token_cache=None):
        self.api = api
        self.raw = raw_message
        body = raw_message.get('body') or {}
        self.message_id = str(body.get('mid') or '')
        self.mid = self.message_id
        self.text = body.get('text') or ''
        recipient = raw_message.get('recipient') or {}
        sender = raw_message.get('sender') or {}
        self.from_user = _User(sender.get('user_id'))
        self.chat = _Chat(recipient.get('chat_type', 'dialog'))
        self.chat_id = recipient.get('chat_id')
        self.recipient_user_id = recipient.get('user_id') or sender.get('user_id')
        self._banner_cache = banner_token_cache
        attachments = body.get('attachments') or []
        self.photo = next((a for a in attachments if a.get('type') == 'image'), None)
        self.contact = None
        contact_att = next((a for a in attachments if a.get('type') == 'contact'), None)
        if contact_att:
            payload = contact_att.get('payload') or {}
            vcf = payload.get('vcf_info') or ''
            match = TEL_RE.search(vcf)
            phone = match.group(1).strip() if match else None
            max_info = payload.get('max_info') or {}
            self.contact = SimpleNamespace(
                phone_number=phone,
                user_id=max_info.get('user_id'),
            )

    # ---- outgoing ----

    async def answer(self, text, reply_markup=None):
        return await self.api.send(user_id=self.recipient_user_id,
                                   chat_id=None if self.recipient_user_id is not None else self.chat_id,
                                   text=text, attachments=_attachment(reply_markup))

    async def answer_photo(self, image_token, caption, reply_markup=None):
        attachments = []
        if image_token:
            attachments.append({'type': 'image',
                                'payload': {'token': image_token}})
        kb = _attachment(reply_markup)
        if kb:
            attachments.append(kb)
        return await self.api.send(user_id=self.recipient_user_id,
                                   chat_id=None if self.recipient_user_id is not None else self.chat_id,
                                   text=caption, attachments=attachments or None)

    async def _edit(self, text, reply_markup, keep_image=True):
        attachments = []
        if keep_image and self.photo and isinstance(self._banner_cache, dict):
            token = (self.photo.get('payload') or {}).get('token') \
                or self._banner_cache.get('token')
            if token:
                attachments.append({'type': 'image', 'payload': {'token': token}})
        kb = _attachment(reply_markup)
        if kb:
            attachments.append(kb)
        return await self.api.edit(self.message_id, text=text,
                                   attachments=attachments or None)

    async def edit_text(self, text, reply_markup=None):
        try:
            return await self._edit(text, reply_markup)
        except Exception as e:
            if getattr(e, 'status', None) in (400, 404):
                return None
            raise

    async def edit_caption(self, caption, reply_markup=None):
        return await self.edit_text(caption, reply_markup)

    async def edit_reply_markup(self, reply_markup=None):
        return await self.edit_text(self.text or '', reply_markup)


class MaxCallback:
    """Wraps message_callback update. message may be None if deleted."""

    def __init__(self, api, update, banner_token_cache=None):
        self.api = api
        callback = update.get('callback') or {}
        self.callback_id = callback.get('callback_id')
        self.data = callback.get('payload') or ''
        user = callback.get('user') or {}
        self.from_user = _User(user.get('user_id'))
        self.message = None
        raw_message = update.get('message')
        if raw_message:
            self.message = MaxMessage(api, raw_message,
                                      banner_token_cache=banner_token_cache)

    async def answer(self, text=None, show_alert=False):
        if self.callback_id:
            try:
                return await self.api.answer(self.callback_id,
                                             notification=text)
            except Exception:
                return None
        return None
