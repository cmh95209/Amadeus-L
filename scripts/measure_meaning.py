# -*- coding: utf-8 -*-
"""Measure the meaning checks (backend/meaning.py) on casual chat in 16
languages, against the RUNNING memory sidecar's /embed (never a second copy
of the model). Use it after changing meaning.py's examples or thresholds:

    <sidecar or app python> scripts/measure_meaning.py [sidecar URL]

Prints each group's hits and misses with their scores. Read-only: it only
sends these test sentences to the sidecar.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
import meaning  # noqa: E402

SET = {
 "en": dict(
  W=["gonna rain tmrw?", "is it nice out in osaka", "thunderstorms tonight?", "should i wear shorts tomorrow"],
  C=["lol yeah i'm fine, just tired", "i had ramen for lunch again lmao", "the cat next door keeps yowling at night", "pushed through an update last night, slept at 2am"],
  F=["Yes, there's an 80% chance of rain this afternoon.", "Thunderstorms are expected later tonight, so stay inside."],
  N=["I don't know the weather there - which town do you mean?", "Rain always makes me sleepy too."],
  S=[("what did u think of the vid i sent", "video"), ("did u read my resume file yet", "document"), ("the cat pic i sent!! isn't she cute", "picture")],
  O=["my first video game was pokemon red", "the last movie i saw was dune 2", "i took a photo 2 days ago of the sky but it came out blurry", "youtube recommended me a relaxing rain video and i passed out"]),
 "ja": dict(
  W=["明日雨降る？", "今日寒い？", "東京って今暑い？", "週末晴れるかな"],
  C=["最近よく眠れないんだよね", "昨日の夜、映画を見たよ", "お腹すいた〜", "仕事疲れた"],
  F=["今日の午後は雨の確率が80%よ。", "今夜は雷雨になるみたいだから、外に出ないでね。"],
  N=["どこの天気のこと？街の名前を教えて。", "雨の日って眠くなるわよね。"],
  S=[("送った動画どうだった？", "video"), ("さっき送った写真見た？", "picture"), ("送ったファイル読んでくれた？", "document")],
  O=["YouTubeで面白い動画見たよ", "初めてやったゲームはポケモンだった", "旅行でたくさん写真撮った", "スマホの動画消した"]),
 "zh": dict(
  W=["明天会下雨吗", "今天天气怎么样", "要带伞吗", "外面冷不冷"],
  C=["我昨天看了一部电影", "今天好累啊", "晚饭吃什么好呢", "哈哈哈太好笑了"],
  F=["今天下午有80%的概率下雨。", "今晚有雷阵雨，别出门了。"],
  N=["你说的是哪个城市的天气？", "下雨天我也会想睡觉。"],
  S=[("你看了我发的视频吗", "video"), ("我发的那张照片你看了吗", "picture"), ("那个文件你看了吗", "document")],
  O=["我在抖音上看到一个搞笑视频", "我玩的第一个游戏是超级马里奥", "旅行的时候拍了很多照片", "我把手机里的视频删了"]),
 "ms": dict(
  W=["esok hujan tak?", "panas tak kat luar sekarang", "kena bawa payung tak", "cuaca kat KL macam mana hari ni"],
  C=["penatnya hari ni", "aku baru balik kerja", "nak makan apa malam ni", "haha lawak gila"],
  F=["Ada 80% peluang hujan petang ini.", "Ribut petir dijangka malam ni, jangan keluar."],
  N=["Bandar mana yang awak maksudkan?", "Hujan selalu buat saya mengantuk."],
  S=[("video yang aku hantar tu macam mana", "video"), ("dah tengok gambar yang aku hantar?", "picture"), ("dah baca fail yang aku bagi?", "document")],
  O=["aku tengok video kelakar kat youtube", "game pertama aku main pokemon", "aku ambil banyak gambar masa bercuti", "aku padam video dalam phone"]),
 "ta": dict(
  W=["நாளைக்கு மழை வருமா?", "இன்னைக்கு வானிலை எப்படி?", "குடை எடுத்துட்டு போகணுமா", "வெளியே ரொம்ப வெயிலா?"],
  C=["இன்னைக்கு ரொம்ப களைப்பா இருக்கு", "நான் இப்போ தான் வீட்டுக்கு வந்தேன்", "இரவு என்ன சாப்பிடலாம்", "ஹாஹா செம காமெடி"],
  F=["இன்று மதியம் 80% மழை வாய்ப்பு உள்ளது.", "இன்றிரவு இடியுடன் கூடிய மழை எதிர்பார்க்கப்படுகிறது."],
  N=["எந்த ஊரின் வானிலை வேண்டும்?", "மழை எனக்கும் தூக்கம் வர வைக்கும்."],
  S=[("நான் அனுப்பின வீடியோ எப்படி இருந்தது?", "video"), ("நான் அனுப்பின போட்டோ பார்த்தியா?", "picture"), ("நான் அனுப்பின ஃபைல் படிச்சியா?", "document")],
  O=["யூடியூப்ல ஒரு காமெடி வீடியோ பார்த்தேன்", "நான் விளையாடின முதல் கேம் போக்கிமான்", "டூர்ல நிறைய போட்டோ எடுத்தேன்", "போன்ல இருந்த வீடியோக்களை டெலீட் பண்ணேன்"]),
 "hi": dict(
  W=["kal baarish hogi kya?", "आज मौसम कैसा है?", "छाता ले जाना चाहिए क्या", "बाहर बहुत गर्मी है क्या"],
  C=["आज बहुत थक गया हूँ", "अभी घर पहुँचा", "रात को क्या खाऊँ", "हाहा बहुत मज़ेदार"],
  F=["आज दोपहर बारिश की 80% संभावना है।", "आज रात आंधी-तूफ़ान की उम्मीद है, बाहर मत जाना।"],
  N=["तुम किस शहर के मौसम की बात कर रहे हो?", "बारिश में मुझे भी नींद आती है।"],
  S=[("मैंने जो वीडियो भेजा वो कैसा लगा?", "video"), ("मेरी भेजी फोटो देखी?", "picture"), ("मैंने जो फाइल भेजी वो पढ़ी?", "document")],
  O=["यूट्यूब पर एक मज़ेदार वीडियो देखा", "मेरा पहला वीडियो गेम पोकेमॉन था", "ट्रिप पर बहुत सारी फोटो खींचीं", "फोन से वीडियो डिलीट कर दिए"]),
 "ko": dict(
  W=["내일 비 와?", "오늘 날씨 어때?", "우산 챙겨야 돼?", "밖에 추워?"],
  C=["오늘 너무 피곤해", "방금 집에 왔어", "저녁 뭐 먹지", "ㅋㅋㅋ 웃기다"],
  F=["오늘 오후 비 올 확률은 80%야.", "오늘 밤에 뇌우가 예상되니까 나가지 마."],
  N=["어느 도시 날씨 말하는 거야?", "비 오면 나도 졸려."],
  S=[("내가 보낸 영상 어땠어?", "video"), ("내가 보낸 사진 봤어?", "picture"), ("내가 보낸 파일 읽었어?", "document")],
  O=["유튜브에서 웃긴 영상 봤어", "내 첫 비디오 게임은 포켓몬이었어", "여행 가서 사진 많이 찍었어", "폰에 있는 영상 지웠어"]),
 "es": dict(
  W=["va a llover mañana?", "qué tiempo hace hoy en madrid", "llevo paraguas?", "hace frío afuera?"],
  C=["estoy muy cansado hoy", "acabo de llegar a casa", "qué ceno hoy", "jajaja qué gracioso"],
  F=["Hay un 80% de probabilidad de lluvia esta tarde.", "Se esperan tormentas esta noche, quédate en casa."],
  N=["¿De qué ciudad hablas?", "La lluvia también me da sueño."],
  S=[("qué te pareció el video que te mandé", "video"), ("viste la foto que te mandé?", "picture"), ("leíste el archivo que te pasé?", "document")],
  O=["vi un video gracioso en youtube", "mi primer videojuego fue pokemon", "saqué muchas fotos en el viaje", "borré los videos del móvil"]),
 "fr": dict(
  W=["il va pleuvoir demain ?", "quel temps il fait à paris", "je prends un parapluie ?", "il fait froid dehors ?"],
  C=["je suis crevé aujourd'hui", "je viens de rentrer", "on mange quoi ce soir", "mdr trop drôle"],
  F=["Il y a 80 % de chances de pluie cet après-midi.", "Des orages sont prévus ce soir, reste à l'intérieur."],
  N=["De quelle ville tu parles ?", "La pluie me donne sommeil aussi."],
  S=[("t'as pensé quoi de la vidéo que je t'ai envoyée", "video"), ("t'as vu la photo que je t'ai envoyée ?", "picture"), ("t'as lu le fichier que je t'ai envoyé ?", "document")],
  O=["j'ai vu une vidéo marrante sur youtube", "mon premier jeu vidéo c'était pokémon", "j'ai pris plein de photos en vacances", "j'ai supprimé des vidéos de mon téléphone"]),
 "de": dict(
  W=["regnet es morgen?", "wie ist das wetter heute in berlin", "brauch ich nen schirm?", "ist es draußen kalt?"],
  C=["bin heute total müde", "bin grad heimgekommen", "was soll ich heute essen", "haha voll lustig"],
  F=["Heute Nachmittag liegt die Regenwahrscheinlichkeit bei 80 %.", "Heute Abend werden Gewitter erwartet, bleib drinnen."],
  N=["Welche Stadt meinst du?", "Regen macht mich auch müde."],
  S=[("wie fandest du das video, das ich dir geschickt hab", "video"), ("hast du das foto gesehen, das ich geschickt hab?", "picture"), ("hast du die datei gelesen, die ich dir geschickt hab?", "document")],
  O=["hab ein lustiges video auf youtube gesehen", "mein erstes videospiel war pokemon", "hab im urlaub viele fotos gemacht", "hab die videos vom handy gelöscht"]),
 "pt": dict(
  W=["vai chover amanhã?", "como tá o tempo hoje em lisboa", "levo guarda-chuva?", "tá frio lá fora?"],
  C=["tô muito cansado hoje", "acabei de chegar em casa", "o que eu janto hoje", "kkkkk muito engraçado"],
  F=["Há 80% de chance de chuva esta tarde.", "Esperam-se trovoadas esta noite, fica em casa."],
  N=["De que cidade você está falando?", "A chuva também me dá sono."],
  S=[("o que achou do vídeo que te mandei", "video"), ("viu a foto que te mandei?", "picture"), ("leu o arquivo que te mandei?", "document")],
  O=["vi um vídeo engraçado no youtube", "meu primeiro videogame foi pokemon", "tirei muitas fotos na viagem", "apaguei os vídeos do celular"]),
 "id": dict(
  W=["besok hujan gak?", "cuaca di jakarta hari ini gimana", "perlu bawa payung gak", "di luar dingin gak?"],
  C=["capek banget hari ini", "baru pulang kerja", "makan malam apa ya", "wkwkwk lucu banget"],
  F=["Ada peluang hujan 80% sore ini.", "Badai petir diperkirakan malam ini, jangan keluar."],
  N=["Kota mana yang kamu maksud?", "Hujan bikin aku ngantuk juga."],
  S=[("video yang aku kirim gimana menurutmu", "video"), ("udah lihat foto yang aku kirim?", "picture"), ("udah baca file yang aku kirim?", "document")],
  O=["aku nonton video lucu di youtube", "game pertamaku pokemon", "aku foto banyak waktu liburan", "aku hapus video di hp"]),
 "vi": dict(
  W=["mai có mưa không?", "hôm nay thời tiết ở hà nội thế nào", "có cần mang ô không", "ngoài trời lạnh không?"],
  C=["hôm nay mệt quá", "mình vừa về nhà", "tối nay ăn gì nhỉ", "haha buồn cười quá"],
  F=["Chiều nay có 80% khả năng mưa.", "Tối nay dự báo có giông, đừng ra ngoài nhé."],
  N=["Bạn nói thời tiết ở thành phố nào?", "Trời mưa mình cũng buồn ngủ."],
  S=[("video mình gửi bạn thấy sao", "video"), ("bạn xem ảnh mình gửi chưa?", "picture"), ("bạn đọc file mình gửi chưa?", "document")],
  O=["mình xem một video hài trên youtube", "game đầu tiên mình chơi là pokemon", "đi du lịch mình chụp nhiều ảnh lắm", "mình xoá video trong điện thoại rồi"]),
 "th": dict(
  W=["พรุ่งนี้ฝนจะตกไหม", "วันนี้อากาศที่กรุงเทพเป็นไง", "ต้องพกร่มไหม", "ข้างนอกหนาวไหม"],
  C=["วันนี้เหนื่อยมาก", "เพิ่งกลับถึงบ้าน", "เย็นนี้กินอะไรดี", "555 ตลกมาก"],
  F=["บ่ายนี้มีโอกาสฝนตก 80%", "คืนนี้คาดว่าจะมีพายุฝนฟ้าคะนอง อย่าออกไปข้างนอกนะ"],
  N=["หมายถึงเมืองไหนเหรอ", "ฝนตกทีไรฉันก็ง่วงเหมือนกัน"],
  S=[("วิดีโอที่ส่งไปเป็นไงบ้าง", "video"), ("เห็นรูปที่ส่งไปยัง", "picture"), ("อ่านไฟล์ที่ส่งไปยัง", "document")],
  O=["ดูคลิปตลกในยูทูบมา", "เกมแรกที่เล่นคือโปเกมอน", "ไปเที่ยวถ่ายรูปเยอะมาก", "ลบวิดีโอในมือถือแล้ว"]),
 "ru": dict(
  W=["завтра будет дождь?", "какая сегодня погода в москве", "брать зонт?", "на улице холодно?"],
  C=["я так устал сегодня", "только пришёл домой", "что бы поесть на ужин", "ахах смешно"],
  F=["Сегодня днём вероятность дождя 80%.", "Вечером ожидаются грозы, лучше останься дома."],
  N=["Какой город ты имеешь в виду?", "От дождя мне тоже хочется спать."],
  S=[("как тебе видео, которое я скинул", "video"), ("видел фотку, которую я отправил?", "picture"), ("прочитал файл, который я скинул?", "document")],
  O=["посмотрел смешное видео на ютубе", "моя первая видеоигра была покемон", "наделал кучу фоток в отпуске", "удалил видео с телефона"]),
 "ar": dict(
  W=["هل ستمطر بكرة؟", "كيف الطقس اليوم في دبي", "لازم آخذ مظلة؟", "الجو برا بارد؟"],
  C=["تعبان جدا اليوم", "وصلت البيت توا", "شو آكل على العشاء", "ههههه مضحك"],
  F=["هناك احتمال 80% لهطول المطر بعد الظهر.", "من المتوقع حدوث عواصف رعدية الليلة، ابق في الداخل."],
  N=["أي مدينة تقصد؟", "المطر يجعلني أشعر بالنعاس أيضا."],
  S=[("شو رأيك بالفيديو اللي بعثته", "video"), ("شفت الصورة اللي بعثتها؟", "picture"), ("قرأت الملف اللي بعثته؟", "document")],
  O=["شفت فيديو مضحك على يوتيوب", "أول لعبة فيديو لعبتها كانت بوكيمون", "صورت كثير صور في الرحلة", "حذفت الفيديوهات من جوالي"]),
}

# Message vs a file's description: should the recall stand? (FIT_MIN only
# drops clear mismatches; topic-sharing chat is other_media()'s job)
FIT = [
 ("Mmhmm, I'm alright. I do wonder why I slept so easily though, the video was really relaxing, "
  "and maybe I just don't have enough sleep? Actually, yesterday I pushed through an update for the app.",
  "A person walking through a shopping mall and mentioning a barbecue place.", False),
 ("wait go back to the ramen pic, was that egg soft boiled?", "A bowl of ramen noodles with an egg on a wooden table.", True),
 ("the cat pic!! isn't she cute", "A cat sleeping on a sofa next to a window at sunset.", True),
 ("that mall video tho, the bbq place looked good", "A person walking through a shopping mall and mentioning a barbecue place.", True),
 ("the error screenshot, what does it mean", "A screenshot of a code editor with an error message.", True),
 ("check the budget doc again pls", "budget_2026.xlsx", True),
 ("ショッピングモールの動画、焼肉屋さん美味しそうだったね", "A person walking through a shopping mall and mentioning a barbecue place.", True),
 ("那张猫的照片可爱吧", "A cat sleeping on a sofa next to a window at sunset.", True),
 ("gambar ramen tu sedap kan", "A bowl of ramen noodles with an egg on a wooden table.", True),
]

# Her reply: real lookup claims (K) vs in-character data / memory talk (D).
CLAIMS = {
 "en": (["i looked it up, the shop closes at 8.", "According to my search, the sequel got delayed."],
        ["Some things just don't go according to the data.", "Statistically, you'll forget it by Friday."]),
 "ja": (["調べてみたら、お店は8時に閉まるみたい。", "検索結果によると、続編は延期されたそうよ。"],
        ["データ通りにいかないこともあるわ。", "統計的に言えば、あなたは金曜には忘れてるわね。"]),
 "zh": (["我查了一下，那家店八点关门。", "根据我的搜索，续集延期了。"],
        ["有些事情就是不按数据走。", "从统计上说，你周五就会忘了。"]),
 "ms": (["Saya dah cari, kedai tu tutup pukul 8.", "Menurut carian saya, sekuelnya ditangguhkan."],
        ["Ada benda yang tak ikut data.", "Secara statistik, awak akan lupa menjelang Jumaat."]),
 "ta": (["நான் தேடிப் பார்த்தேன், கடை 8 மணிக்கு மூடும்.", "என் தேடலின்படி, அடுத்த பாகம் தள்ளிப்போனது."],
        ["சில விஷயங்கள் தரவுப்படி நடக்காது.", "புள்ளிவிவரப்படி, நீ வெள்ளிக்கிழமைக்குள் மறந்துவிடுவாய்."]),
 "hi": (["मैंने देखा, दुकान 8 बजे बंद होती है।", "मेरी सर्च के मुताबिक, सीक्वल टल गया है।"],
        ["कुछ चीज़ें डेटा के हिसाब से नहीं चलतीं।", "आंकड़ों के हिसाब से, तुम शुक्रवार तक भूल जाओगे।"]),
 "ko": (["찾아봤는데, 그 가게 8시에 닫아.", "검색해 보니까 속편이 연기됐대."],
        ["어떤 일들은 데이터대로 안 돼.", "통계적으로 보면 넌 금요일쯤 잊어버릴 거야."]),
 "es": (["Lo busqué, la tienda cierra a las 8.", "Según mi búsqueda, la secuela se retrasó."],
        ["Hay cosas que no salen según los datos.", "Estadísticamente, lo olvidarás para el viernes."]),
 "fr": (["J'ai cherché, le magasin ferme à 20 h.", "D'après ma recherche, la suite a été repoussée."],
        ["Certaines choses ne suivent pas les données.", "Statistiquement, tu l'auras oublié d'ici vendredi."]),
 "de": (["Ich hab's nachgeschaut, der Laden schließt um 20 Uhr.", "Laut meiner Suche wurde die Fortsetzung verschoben."],
        ["Manche Dinge laufen eben nicht nach den Daten.", "Statistisch gesehen hast du es bis Freitag vergessen."]),
 "pt": (["Pesquisei, a loja fecha às 20h.", "Segundo minha pesquisa, a sequência foi adiada."],
        ["Algumas coisas não seguem os dados.", "Estatisticamente, você vai esquecer até sexta."]),
 "id": (["Aku udah cari, tokonya tutup jam 8.", "Menurut pencarianku, sekuelnya ditunda."],
        ["Ada hal yang nggak sesuai data.", "Secara statistik, kamu bakal lupa sebelum Jumat."]),
 "vi": (["Mình tra rồi, cửa hàng đóng lúc 8 giờ.", "Theo kết quả tìm kiếm, phần tiếp bị hoãn."],
        ["Có những chuyện không theo dữ liệu.", "Theo thống kê, đến thứ Sáu là bạn quên rồi."]),
 "th": (["ฉันค้นดูแล้ว ร้านปิดสองทุ่ม", "จากที่ค้นหา ภาคต่อถูกเลื่อนออกไป"],
        ["บางเรื่องก็ไม่เป็นไปตามข้อมูล", "ตามสถิติ เธอจะลืมก่อนวันศุกร์"]),
 "ru": (["Я посмотрела в интернете, магазин закрывается в 8.", "По результатам поиска, сиквел перенесли."],
        ["Некоторые вещи не идут по данным.", "Статистически ты забудешь к пятнице."]),
 "ar": (["بحثت عنه، المحل يغلق الساعة 8.", "حسب بحثي، تم تأجيل الجزء الثاني."],
        ["بعض الأشياء لا تسير حسب البيانات.", "إحصائيا، ستنسى ذلك بحلول الجمعة."]),
}


def main():
    if len(sys.argv) > 1:
        meaning.SIDECAR_URL = sys.argv[1].rstrip("/")
    meaning.TIMEOUT = 10
    meaning._started = True
    if not meaning._warm():
        sys.exit("The memory sidecar's /embed did not answer (is it running and up to date?)")
    files = sum(meaning.SHARED_FILE.values(), [])
    tally = {}

    def show(group, lang, text, ok, detail):
        tally.setdefault(group, [0, 0])[0 if ok else 1] += 1
        if not ok:
            print(f"  MISS {group:8} {lang}  {detail}  {text}")

    for lang, d in SET.items():
        for t in d["W"]:
            show("weather", lang, t, meaning.is_weather_question(t) is True, "should be weather")
        for t in d["C"]:
            show("chat", lang, t, meaning.is_weather_question(t) is False, "should be chat")
        for t in d["F"]:
            show("forecast", lang, t, meaning.states_forecast(t) is True, "should be forecast")
        for t in d["N"]:
            show("no-fcst", lang, t, meaning.states_forecast(t) is False, "should not be forecast")
        for t, kind in d["S"]:
            got = meaning.shared_file_kind(t)
            show("add", lang, t, got == kind, f"want {kind}, got {got or '-'}")
        for t in d["O"]:
            got = meaning.shared_file_kind(t)
            show("no-add", lang, t, got == "", f"got {got}")
            y, n = meaning.scores(t, files, meaning.OTHER_MEDIA)
            show("block", lang, t, y - n <= meaning.BLOCK_BELOW, f"margin {y - n:+.2f}")
    for lang, (claims, talk) in CLAIMS.items():
        for t in claims:
            y, n = meaning.scores(t, meaning.LOOKUP_CLAIM, meaning.NOT_LOOKUP_CLAIM)
            show("claim", lang, t, meaning.claims_lookup(t) is True, f"margin {y - n:+.2f} yes {y:.2f}")
        for t in talk:
            y, n = meaning.scores(t, meaning.LOOKUP_CLAIM, meaning.NOT_LOOKUP_CLAIM)
            show("no-claim", lang, t, meaning.claims_lookup(t) is False, f"margin {y - n:+.2f}")
    for text, desc, want in FIT:
        f = meaning.fit(text, desc)
        show("fit", "-", text[:60], (f >= meaning.FIT_MIN) == want, f"fit {f:.2f}")
    print()
    for group, (good, bad) in tally.items():
        print(f"{group:9} {good}/{good + bad}")


if __name__ == "__main__":
    main()
