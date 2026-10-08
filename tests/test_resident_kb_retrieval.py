"""Качество поиска по базе знаний на живых формулировках жителей.

Повод: вопрос «во сколько завтра УК открывается» тянул в контекст модели
шлагбаум, лифт и аварийку раньше графика УК. Причины — бонус за «любое
сильное слово в вопросе + любое в записи» и равный вес слов из текста ответа
(«обратитесь в УК» есть в десятках записей). Эти пороги держат исправление:
нужная запись всегда доходит до модели, а мусора в контексте мало.

Набор TUNING использовался при настройке (синонимы в базе подбирались по его
промахам), HELDOUT — контрольный, написан до правки базы.
"""

from __future__ import annotations

import pytest

from app.services.ai_module import _apply_kb_budget
from app.services.resident_kb import (
    build_resident_context,
    load_resident_kb,
    search_resident_kb,
)

TUNING = [
    ("во сколько завтра ук открывается", "uk_schedule"),
    ("ук в субботу работает?", "uk_schedule"),
    ("подскажите номер управляйки", "uk_contacts"),
    ("где офис ук находится", "uk_contacts"),
    ("какая почта у ук, хочу письмо написать", "uk_contacts"),
    ("у меня течет с потолка что делать", "eds_and_emergency"),
    ("прорвало трубу ночью куда звонить", "eds_and_emergency"),
    ("пропал свет во всем доме", "electricity_contact"),
    ("куда платить за электричество", "electricity_contact"),
    ("пришла огромная сумма за воду хотя нас не было месяц", "water_electricity_recalculation"),
    ("до какого числа сдавать счетчики", "meter_submission"),
    ("как передать показания воды", "meter_submission"),
    ("лифт застрял между этажами", "lift_service"),
    ("лифт опять сломан в 3 корпусе", "lift_service"),
    ("какой тут интернет можно провести", "internet_provider"),
    ("кто наш участковый", "district_officer"),
    ("соседи дерутся, полицию как вызвать", "district_officer"),
    ("как посмотреть камеры во дворе", "video_surveillance"),
    ("как добавить машину чтобы шлагбаум открывался", "gate_personal_car"),
    ("купил новую машину как вписать номер в шлагбаум", "gate_personal_car"),
    ("как заказать пропуск для гостя", "guest_pass_flow"),
    ("ко мне едет курьер как его пустить через шлагбаум", "guest_pass_flow"),
    ("номер машины гостя не знаю как быть", "guest_pass_without_number"),
    ("кнопка открыть в дворецком не работает", "gate_open_button"),
    ("не приходит смс код в дворецкий", "dvoretsky_auth"),
    ("как скачать приложение для шлагбаума", "gate_apps"),
    ("можно ли рекламу в чате", "chat_rules"),
    ("где ближайшая поликлиника для взрослых", "polyclinic"),
    ("к педиатру куда записаться", "children_polyclinic"),
    ("ребенок ногу подвернул где травмпункт", "hospital_trauma"),
    ("зуб болит куда идти", "stomatology"),
    ("в какую школу записывать ребенка", "school"),
    ("как записать в первый класс", "school_enrollment"),
    ("есть ли рядом садик", "kindergarten"),
    ("как добраться до москвы на электричке", "transport_train"),
    ("сколько ехать до мкад на машине", "transport_roads"),
    ("до скольки можно сверлить в выходные", "noise_law"),
    ("сосед делает ремонт в 10 вечера это законно?", "noise_law"),
    ("где мфц", "administration_mfc"),
    ("где тут погулять с коляской", "parks_nature"),
    ("можно ли выгуливать собаку без поводка во дворе", "animals_rules"),
    ("можно ставить велосипед на лестничной площадке", "fire_safety"),
    ("куда выкинуть старый диван", "trash_disposal"),
    ("ук ничего не делает куда пожаловаться", "housing_inspection"),
    ("дайте все важные телефоны", "useful_contacts_summary"),
    ("какой код от домофона", "intercom_access"),
    ("батареи холодные когда дадут тепло", "heating"),
    ("доставка не может заехать во двор", "delivery_couriers"),
    ("когда общее собрание собственников", "owners_meeting"),
    ("как подать заявку через приложение ук", "uk_app"),
    ("нет горячей воды с утра", "water_supply"),
    ("можно оставить каршеринг во дворе", "carsharing_taxi"),
    ("где можно бесплатно парковаться", "parking_rules"),
    ("вызвать скорую на адрес жк", "ambulance_local"),
    ("в подъезде пахнет газом", "gas_emergency"),
    ("как проверить начисления ук в гис жкх", "uk_gis_control"),
    ("можно ли купить кладовку", "storage_rooms"),
    ("как зарегистрировать право собственности на квартиру", "property_registration"),
    ("как прописаться в новой квартире", "residence_registration"),
    ("хочу снести стену между кухней и комнатой", "renovation_approval"),
    ("нас залили соседи сверху что делать", "flooding_insurance"),
    ("где почта россии", "post_office"),
    ("где детская площадка", "playgrounds"),
    ("что за жк живописный где он", "about_complex"),
    ("какие законы защищают жильцов", "housing_legislation"),
]

HELDOUT = [
    ("а в понедельник ук принимает?", "uk_schedule"),
    ("до скольки сегодня управляющая компания", "uk_schedule"),
    ("телефон управляющей компании век", "uk_contacts"),
    ("затопило ванную срочно", "eds_and_emergency"),
    ("кому звонить если ночью авария", "eds_and_emergency"),
    ("счет за свет куда оплачивать", "electricity_contact"),
    ("мы не жили в квартире а воду начислили", "water_electricity_recalculation"),
    ("где передавать показания счетчиков электричества", "meter_submission"),
    ("застряла в лифте с ребенком", "lift_service"),
    ("какой провайдер в доме", "internet_provider"),
    ("номер участкового полицейского", "district_officer"),
    ("где смотреть видео с камер у подъезда", "video_surveillance"),
    ("как прописать авто в шлагбауме", "gate_personal_car"),
    ("гостевой пропуск на такси", "guest_pass_flow"),
    ("шлагбаум не открывается через приложение", "gate_open_button"),
    ("не могу войти в дворецкий", "dvoretsky_auth"),
    ("что запрещено писать в чате", "chat_rules"),
    ("взрослая поликлиника где", "polyclinic"),
    ("детский врач где принимает", "children_polyclinic"),
    ("где ближайшая больница", "hospital_trauma"),
    ("хороший стоматолог рядом", "stomatology"),
    ("какая школа у нас по прописке", "school"),
    ("запись в садик как", "kindergarten"),
    ("расписание электричек до москвы", "transport_train"),
    ("до скольки можно шуметь в будни", "noise_law"),
    ("где получить паспорт мфц", "administration_mfc"),
    ("куда выгулять собаку", "animals_rules"),
    ("вывоз строительного мусора", "trash_disposal"),
    ("куда писать жалобу на управляющую компанию", "housing_inspection"),
    ("подскажите код подъезда", "intercom_access"),
    ("когда включат батареи", "heating"),
    ("курьер не может проехать", "delivery_couriers"),
    ("голосование собственников как участвовать", "owners_meeting"),
    ("горячей воды нет", "water_supply"),
    ("где оставить машину гостю", "parking_rules"),
    ("чувствую запах газа на кухне", "gas_emergency"),
    ("продают ли кладовые", "storage_rooms"),
    ("как сделать регистрацию по месту жительства", "residence_registration"),
    ("нужно ли согласовывать перенос стены", "renovation_approval"),
    ("сосед сверху затопил что делать", "flooding_insurance"),
]


def _measure(cases):
    load_resident_kb.cache_clear()
    answers = {e.id: e.answer for e in load_resident_kb()}
    top1 = top3 = in_prompt = entries = 0
    misses = []
    for question, expected in cases:
        ids = [m.entry.id for m in search_resident_kb(question, top_k=6).matches]
        context = build_resident_context(question)
        prompt = "\n".join(_apply_kb_budget([("resident_canonical", context)]))
        top1 += ids[:1] == [expected]
        top3 += expected in ids[:3]
        seen = answers[expected][:120] in prompt
        in_prompt += seen
        entries += context.count("Категория:")
        if not seen:
            misses.append((question, expected, ids[:3]))
    n = len(cases)
    return top1 / n, top3 / n, in_prompt / n, entries / n, misses


@pytest.mark.parametrize(("name", "cases", "min_top1"), [
    ("tuning", TUNING, 0.95),
    ("heldout", HELDOUT, 0.85),
])
def test_needed_entry_always_reaches_the_model(name, cases, min_top1) -> None:
    top1, top3, in_prompt, entries, misses = _measure(cases)
    assert in_prompt == 1.0, f"{name}: запись не дошла до модели: {misses}"
    assert top3 >= 0.95, f"{name}: top3={top3:.0%}"
    assert top1 >= min_top1, f"{name}: top1={top1:.0%}"
    # До исправления в контекст шло 4.5 записи на вопрос, теперь ~2.5.
    assert entries <= 3.0, f"{name}: в контексте в среднем {entries:.1f} записи"


def test_keyword_bonus_needs_the_same_keyword() -> None:
    """«ук» в вопросе не даёт бонуса записи про лифт только потому, что
    в ней есть другое сильное слово."""
    from app.services import resident_kb as r

    load_resident_kb.cache_clear()
    by_id = {e.id: e for e in load_resident_kb()}
    query = r._content_tokens("во сколько завтра ук открывается")
    assert r._score_entry(query, by_id["uk_schedule"]) > r._score_entry(query, by_id["lift_service"])
    assert r._score_entry(query, by_id["uk_schedule"]) > r._score_entry(query, by_id["gate_personal_car"])
