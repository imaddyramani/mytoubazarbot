MyTourBazar V188 — Direct B2B + Passenger Profile + Payment Distribution + Transit Note

REPLACE ONLY:
1. bot.py
2. flight_print.py
3. template.py

KEEP UNCHANGED:
- flight_extractor.py = current V186
- smart_assistant.py
- hotel_voucher.py
- print_settings.py
- start.py
- Dockerfile
- assets/
- data/

CURRENT STACK AFTER THIS UPDATE
- bot.py = V188 (includes V187 Air PP selling-fare logic)
- flight_extractor.py = V186
- flight_print.py = V188 (includes V184 airport display + V183 fare reconciliation)
- template.py = V188

1) TOUR B2B BUTTON — DIRECT PRINT
Old:
Generated Tour PDF -> B2B -> another Modify/options screen -> Done

New:
Generated Tour PDF -> tap B2B -> B2B PDF prints directly

Preserved automatically:
- current Basic/Detailed level
- current Quotation/Voucher mode
- cost
- page size
- itinerary/transit/content

Removed:
- MyTourBazar logo
- footer/contact details
- watermark
- MyTourBazar wording
- normal branded terms

Company references become:
our company

2) AIR B2B REPLY — DIRECT WHITE-LABEL PRINT
Reply to a generated Air PDF with:
b2b
B2B
white label
unbranded

Result:
- direct Air B2B PDF
- no MyTourBazar logo
- no MyTourBazar footer/contact bar
- no MyTourBazar watermark
- any MyTourBazar visible text is replaced with "our company"
- top-left neutral label becomes "our company"
- airline/passenger/PNR/fare/airport details remain unchanged

No extra option menu is shown.

3) TOUR PASSENGER PROFILE — NON-ZERO ONLY
Example source:
2 Adults + 1 EB + 0 CWB + 0 CNB

Passenger Profile now:
2 Adults • 1 Extra Bed

It does NOT show:
0 Child
0 CWB
0 CNB

4) TOUR PACKAGE COST — KEEP ALL FIELDS
Adult / Child / CWB / CNB / EB columns remain available in PACKAGE COST.

If a rate is missing or zero:
--

Example:
Adult: INR 35,000 × 2
Child: --
CWB: --
CNB: --
EB: INR 12,000 × 1

A missing rate is never shown as a genuine INR 0.

5) AIR PAYMENT DETAILS — NO INVENTED OTHER SUPPLIER CHARGES
The customer selling difference/markup is distributed proportionally across the
AVAILABLE supplier cost fields.

Example source fields:
Air Fare Charges
Fuel Surcharge (YQ)
Fees and Taxes

Final PDF keeps ONLY those fields and adjusts them so their sum equals the final
customer total.

The bot will NOT create "Other Supplier Charges" itself.

Exception:
If "Other Supplier Charges" genuinely exists in the supplier payment_items, it is
preserved because it is source data.

Regression test:
available fields were adjusted to total exactly INR 68,535 with no invented row.

6) TOUR TRANSIT NOTE
Old long note removed.

New exact note:
NOTE - Please check the original ticket copies for reliable information

It appears immediately under the Transit & Connection Schedule and only when the
transit section exists.

VALIDATION
- bot.py syntax: OK
- flight_print.py syntax: OK
- template.py syntax: OK
- V187 7615 pp test preserved:
  INR 64,908 supplier / 9 pax -> 7615 pp -> INR 68,535 final
- Passenger Profile test:
  2 Adults + 1 EB -> "2 Adults • 1 Extra Bed"
- Air payment rows sum exactly to final customer total
- No invented Other Supplier Charges
- Genuine source-provided Other Supplier Charges remains allowed
