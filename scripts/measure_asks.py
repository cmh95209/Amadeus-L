# -*- coding: utf-8 -*-
"""Measure meaning.py's "asks" checks (search / time / link / again) on
casual texting in 16 languages, against the RUNNING memory sidecar's /embed
(never a second copy of the model). Read-only: it only sends these test
sentences to the sidecar.

    <app python> scripts/measure_asks.py [sidecar URL]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.path.insert(0, HERE)
import meaning  # noqa: E402
import measure_meaning  # noqa: E402

# lang -> (search asks, time ask, link ask, again ask)
ASKS = {
 "en": (["can u search the new zelda release date", "look up if the mall is open today"], "what time is it rn", "send me the link pls", "try again"),
 "ja": (["新しいゼルダの発売日、調べてくれる？", "今日モールが開いてるか検索して"], "今何時？", "リンク送って", "もう一回やってみて"),
 "zh": (["帮我查一下新塞尔达的发售日", "上网搜一下商场今天开不开"], "现在几点了", "把链接发给我", "再试一次"),
 "ko": (["새 젤다 출시일 좀 검색해줘", "오늘 쇼핑몰 여는지 찾아봐"], "지금 몇 시야?", "링크 보내줘", "다시 해봐"),
 "es": (["busca la fecha de salida del nuevo zelda", "mira en internet si el centro comercial abre hoy"], "qué hora es", "pásame el enlace", "inténtalo otra vez"),
 "fr": (["cherche la date de sortie du nouveau zelda", "regarde sur internet si le centre commercial est ouvert"], "il est quelle heure", "envoie-moi le lien", "réessaie"),
 "de": (["such mal das Erscheinungsdatum vom neuen Zelda", "schau online, ob das Einkaufszentrum heute offen hat"], "wie spät ist es", "schick mir den Link", "versuch es nochmal"),
 "pt": (["pesquisa a data de lançamento do novo zelda", "vê na internet se o shopping abre hoje"], "que horas são", "me manda o link", "tenta de novo"),
 "ru": (["поищи дату выхода новой зельды", "посмотри в интернете, работает ли торговый центр"], "который час", "скинь ссылку", "попробуй ещё раз"),
 "ar": (["ابحث عن موعد صدور زيلدا الجديدة", "شوف على النت إذا المول مفتوح اليوم"], "كم الساعة الآن", "أرسل لي الرابط", "حاول مرة أخرى"),
 "hi": (["naye zelda ki release date search karo", "इंटरनेट पर देखो मॉल आज खुला है क्या"], "अभी कितने बजे हैं", "लिंक भेजो", "फिर से कोशिश करो"),
 "ta": (["புது செல்டா ரிலீஸ் தேதியை தேடிப் பாரு", "இன்னைக்கு மால் திறந்திருக்கான்னு இணையத்தில் பாரு"], "இப்போ மணி என்ன", "லிங்க் அனுப்பு", "மறுபடியும் முயற்சி பண்ணு"),
 "ms": (["tolong cari tarikh keluar zelda baru", "tengok kat internet mall buka tak hari ni"], "pukul berapa sekarang", "hantar link tu", "cuba lagi"),
 "id": (["tolong cariin tanggal rilis zelda baru", "cek di internet mal buka nggak hari ini"], "jam berapa sekarang", "kirim linknya dong", "coba lagi"),
 "th": (["ช่วยค้นหาวันวางขายเซลด้าภาคใหม่หน่อย", "เช็คในเน็ตให้หน่อยว่าห้างเปิดไหมวันนี้"], "ตอนนี้กี่โมงแล้ว", "ส่งลิงก์มาหน่อย", "ลองใหม่อีกที"),
 "vi": (["tìm giúp ngày phát hành zelda mới", "xem trên mạng trung tâm thương mại hôm nay có mở không"], "mấy giờ rồi", "gửi link cho mình", "thử lại đi"),
}


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else meaning.SIDECAR_URL
    meaning.SIDECAR_URL = url.rstrip("/")
    meaning._started = True
    if not meaning._warm():
        print("sidecar not reachable:", url)
        return 1
    checks = {"search": meaning.asks_to_search, "time": meaning.asks_time,
              "link": meaning.asks_for_link, "again": meaning.asks_again}
    groups = {"search": meaning.SEARCH_ASK, "time": meaning.TIME_ASK,
              "link": meaning.LINK_ASK, "again": meaning.AGAIN_ASK}
    hit = {k: [0, 0] for k in checks}
    false = {k: [0, 0] for k in checks}
    for lang, (search, tm, link, again) in ASKS.items():
        want = [(t, "search") for t in search] + [(tm, "time"), (link, "link"), (again, "again")]
        for text, kind in want:
            ok = checks[kind](text)
            s = meaning.scores(text, groups[kind], meaning.CHAT + meaning.NOT_ASK)
            hit[kind][1] += 1
            hit[kind][0] += bool(ok)
            if not ok:
                print(f"MISS {kind:6} {lang} {s[0]:.2f}/{s[1]:.2f}  {text}")
        chat = measure_meaning.SET.get(lang, {}).get("C", [])
        for text in chat:
            for kind, fn in checks.items():
                false[kind][1] += 1
                if fn(text):
                    false[kind][0] += 1
                    s = meaning.scores(text, groups[kind], meaning.CHAT + meaning.NOT_ASK)
                    print(f"FALSE {kind:6} {lang} {s[0]:.2f}/{s[1]:.2f}  {text}")
    for k in checks:
        print(f"{k:6}: caught {hit[k][0]}/{hit[k][1]}, ordinary chat taken {false[k][0]}/{false[k][1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
