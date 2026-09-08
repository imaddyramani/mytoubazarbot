import unittest
from unittest.mock import AsyncMock

from telegram.error import BadRequest

from bot import _reply_markdown_with_plain_fallback


class TelegramMarkdownFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_entity_error_retries_same_text_without_parse_mode(self):
        sent=object()
        message=type('Message',(),{})()
        message.reply_text=AsyncMock(side_effect=[
            BadRequest("Can't parse entities: can't find end of the entity starting at byte offset 2273"),
            sent,
        ])
        text='Hotel_Name [supplier text'
        result=await _reply_markdown_with_plain_fallback(message,text,parse_mode='Markdown')
        self.assertIs(result,sent)
        self.assertEqual(message.reply_text.await_count,2)
        self.assertEqual(message.reply_text.await_args_list[0].args,(text,))
        self.assertEqual(message.reply_text.await_args_list[0].kwargs,{'parse_mode':'Markdown'})
        self.assertEqual(message.reply_text.await_args_list[1].args,(text,))
        self.assertEqual(message.reply_text.await_args_list[1].kwargs,{})

    async def test_other_telegram_errors_are_not_hidden(self):
        message=type('Message',(),{})()
        message.reply_text=AsyncMock(side_effect=BadRequest('Message is too long'))
        with self.assertRaises(BadRequest):
            await _reply_markdown_with_plain_fallback(message,'text',parse_mode='Markdown')
        self.assertEqual(message.reply_text.await_count,1)


if __name__=='__main__': unittest.main()
