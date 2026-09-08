import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class AddCostFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_hotel_add_cost_button_opens_persistent_input_state(self):
        message=SimpleNamespace()
        query=SimpleNamespace(data='fare_add:hotel',answer=AsyncMock(),message=message)
        update=SimpleNamespace(callback_query=query)
        context=SimpleNamespace(user_data={
            'pending_hotel_data':{'hotel_name':'Test Hotel'},
            'pending_fare_supplier_total':0,
        })
        with patch('bot.is_allowed',return_value=True), \
             patch('bot._cancel_auto_print'), \
             patch('bot.safe_callback_edit',new=AsyncMock()) as edit:
            await bot.callback_handler(update,context)
        self.assertEqual(context.user_data['pending_fare_kind'],'hotel')
        edit.assert_awaited_once()

    async def test_hotel_add_cost_reply_calculates_and_prints(self):
        message=SimpleNamespace(reply_text=AsyncMock())
        context=SimpleNamespace(user_data={
            'pending_fare_kind':'hotel','pending_fare_supplier_total':0,
            'pending_hotel_data':{
                'hotel_name':'Test Hotel','check_in':'11 Sep 2026','check_out':'14 Sep 2026',
                'nights':'','room_count':2,'extra_bed_count':0,
            },
        })
        with patch('bot._cancel_auto_print'), patch('bot.ask_footer_choice',new=AsyncMock()) as print_pdf:
            handled=await bot._apply_pending_fare_input(message,context,'hotel','3500 per night')
        self.assertTrue(handled)
        self.assertNotIn('pending_fare_kind',context.user_data)
        self.assertEqual(context.user_data['pending_hotel_fare'],10500)
        print_pdf.assert_awaited_once_with(message,context,'hotel')

    async def test_voice_note_uses_the_same_add_cost_handler(self):
        telegram_file=SimpleNamespace(download_to_drive=AsyncMock())
        message=SimpleNamespace(
            voice=SimpleNamespace(file_id='voice-1',mime_type='audio/ogg'),
            reply_to_message=None,reply_text=AsyncMock(),
        )
        context=SimpleNamespace(
            user_data={'pending_fare_kind':'hotel'},
            bot=SimpleNamespace(get_file=AsyncMock(return_value=telegram_file)),
        )
        update=SimpleNamespace(
            message=message,effective_user=SimpleNamespace(id=123),
        )
        with patch('bot.transcribe_voice_note',return_value='3500 per night'), \
             patch('bot.safe_status_edit',new=AsyncMock()), \
             patch('bot._apply_pending_fare_input',new=AsyncMock(return_value=True)) as apply_cost:
            await bot.receive_voice_edit(update,context)
        apply_cost.assert_awaited_once_with(message,context,'hotel','3500 per night')


if __name__=='__main__':
    unittest.main()
