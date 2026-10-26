"""KVKK metinleri: aydınlatma metni ve açık rıza metni (ayrı ayrı).

KVKK Kurulu'nun 2026/347 sayılı ilke kararı gereği aydınlatma ile açık rıza aynı metinde
birleştirilmez: aydınlatma herkese yapılır (toplantıya girince chat'e yazılır, istenirse sesli
okunur), açık rıza ise yalnızca ses izi (biyometrik veri) için, kişinin kendisinden ayrıca
istenir. Metinlerin sürüm özeti rıza kanıtına yazılır; metin değişirse yeni sürüm oluşur.

ÖNEMLİ: Bu metinler şirketin hukuk danışmanı tarafından gözden geçirilmelidir (hukuki sebep,
saklama süresi, yurt dışı aktarım dayanağı). Şirkete özel alanlar ayarlardan gelir.
"""

from __future__ import annotations

import hashlib

from katip.config import get_settings


def _company() -> str:
    return get_settings().company_name or "Şirket"


def _contact() -> str:
    return get_settings().privacy_contact or "toplantıyı düzenleyen kişi"


def retention_text() -> str:
    s = get_settings()
    if s.retention_text:
        return s.retention_text
    if s.retention_days > 0:
        days = s.retention_days
        return f"{days // 30} ay" if days % 30 == 0 or days in (365, 366) else f"{days} gün"
    return "şirketin kişisel veri saklama ve imha politikasında belirtilen süre"


def notice_url() -> str:
    from katip.api.security import absolute_url

    return absolute_url("/gizlilik")


# Sağlayıcı ve barındırma adları toplantıya yazılmaz; konum bilgisi verilir.
def _servers() -> str:
    return get_settings().privacy_server_location


# Ayrılma komutu herkese açıktır (commands.py, Intent.LEAVE: Level.PUBLIC).
_OBJECTION = (
    'Katip\'in toplantıda bulunmasına itiraz ediyorsanız "Katip, toplantıdan çık" demeniz '
    "yeterli; Katip toplantıdan ayrılır."
)
_OBJECTION_CHAT = (
    'Katip\'in toplantıda bulunmasına itiraz ediyorsanız "Katip, toplantıdan çık" demeniz '
    "ya da chat'e /ayrıl yazmanız yeterli; Katip toplantıdan ayrılır."
)


def privacy_notice_chat() -> str:
    """Toplantıya girince chat'e yazılan kısa aydınlatma metni."""
    return (
        f"Bilgilendirme (KVKK aydınlatma metni): Bu toplantıya {_company()} adına yapay zekâ "
        "asistanı Katip katılıyor. Katip geliştirme aşamasındadır. Konuşmalar yazıya dökülüyor "
        "ve çevriliyor; toplantı notu (özet, kararlar, aksiyonlar) hazırlanıyor, istenirse ekran "
        f"görüntüsü alınıyor. Ses kaydı saklanmıyor. {_servers()}; yazıya dökme ve yapay zekâ "
        "işlemleri için veriler yurt dışına aktarılıyor. Sesle kimlik tanıma yapılmaz; "
        "katılımcılar toplantıdaki e-posta bilgileriyle eşleştirilir. Bir bölümün "
        'kaydedilmesini istemezseniz "Katip, kayıt dışı" deyin. '
        f"{_OBJECTION_CHAT} Haklarınız (KVKK md. 11) ve itirazlarınız için: {_contact()}. "
        f"Ayrıntılı metin: {notice_url()} adresinde."
    )


def privacy_notice_spoken() -> str:
    """Sesli okunacak aydınlatma metni (link okunmaz)."""
    return (
        f"Bilgilendirme: Bu toplantıya {_company()} adına yapay zekâ asistanı Katip katılıyor. "
        "Katip geliştirme aşamasındadır. Konuşmalar yazıya dökülüyor, çevriliyor ve toplantı "
        f"notu hazırlanıyor. Ses kaydı saklanmıyor. {_servers()}; bu işlemler için veriler yurt "
        "dışına aktarılıyor. Sesle kimlik tanıma yapılmaz; katılımcılar e-posta bilgileriyle "
        'eşleştirilir. Bir bölümün kaydedilmesini istemezseniz "Katip, kayıt dışı" deyin. '
        f"{_OBJECTION} Ayrıntılı metin toplantı chat'inde."
    )


def late_joiner_notice() -> str:
    return (
        f"Yeni katılanlar için: Bu toplantıda {_company()} adına Katip not alıyor. "
        f"Aydınlatma metni: {notice_url()} adresinde."
    )


_CONSENT = (
    "{name}, bu ayrı bir açık rıza talebidir. Seni sonraki toplantılarda sesinden "
    "tanıyabilmem için sesinden bir ses izi, yani biyometrik veri oluşturup şifreli olarak "
    "saklamak istiyorum. Yalnızca toplantı notlarında konuşmacı olarak adını doğru yazmak için "
    "kullanılır; başka amaçla kullanılmaz. Rıza vermek zorunda değilsin; vermezsen toplantıya "
    "katılımın etkilenmez, adın ekrandaki görünen adınla yazılır. Rızanı istediğin zaman "
    '"Katip, beni unut" diyerek geri alabilirsin; ses izin silinir. Kabul ediyorsan '
    '"onaylıyorum" de.'
)


def consent_text(name: str) -> str:
    return _CONSENT.format(name=name)


def version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


CONSENT_VERSION = version(_CONSENT)


def notice_page_html() -> str:
    """Ayrıntılı aydınlatma metni sayfası (/gizlilik)."""
    import html

    company = html.escape(_company())
    contact = html.escape(_contact())
    retention = html.escape(retention_text())
    servers = html.escape(_servers())
    return f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Katip — Aydınlatma Metni</title>
<style>body{{font:15px/1.6 system-ui,sans-serif;max-width:760px;margin:32px auto;
padding:0 16px;color:#1f2430}}
h1{{font-size:22px}}h2{{font-size:16px;margin-top:24px}}li{{margin:4px 0}}</style></head><body>
<h1>Toplantı asistanı Katip — kişisel verilerin işlenmesine ilişkin aydınlatma metni</h1>
<p>Bu metin, 6698 sayılı Kişisel Verilerin Korunması Kanunu'nun 10. maddesi uyarınca veri
sorumlusu sıfatıyla <b>{company}</b> tarafından hazırlanmıştır.</p>
<h2>1. İşlenen kişisel veriler</h2><ul>
<li>Toplantıdaki konuşmaların yazıya dökülmüş hâli (transkript) ve çevirisi</li>
<li>Toplantıda görünen ad, katılım bilgisi; şirket çalışanları için şirket e-posta adresi</li>
<li>Katip'e aldırılan notlar, kararlar, aksiyonlar ve toplantı özeti</li>
<li>İstendiğinde alınan ekran görüntüleri (paylaşılan ekran)</li>
<li>Katılımcı e-posta adresi ve toplantı içindeki konuşmacı eşleştirmesi</li>
</ul><p>Ses kaydı saklanmaz; ses yalnızca anlık olarak yazıya dökülmek için işlenir.</p>
<h2>2. İşleme amaçları</h2><ul>
<li>Toplantı kaydının ve notlarının tutulması, kararların ve aksiyonların takibi</li>
<li>Canlı çeviri ile çok dilli katılımın sağlanması</li>
<li>Açık rıza verenler için konuşmacının notlarda adıyla doğru yazılması</li></ul>
<h2>3. Hukuki sebepler</h2><ul>
<li>Toplantı kayıtları: KVKK md. 5/2-f (veri sorumlusunun meşru menfaati)</li>
<li>Sesle kimlik tanıma ve yeni ses izi kaydı yapılmaz.</li></ul>
<h2>4. Aktarım</h2>
<p>Katip geliştirme aşamasındadır. {servers}.
Yazıya dökme, çeviri, özet ve asistan cevapları için yurt dışında bulunan yazıya dökme, yapay
zekâ ve barındırma hizmet sağlayıcıları kullanılır. Aktarımlar KVKK md. 9 kapsamında (Kurul'un
standart sözleşmesi ile) yapılır. Ses izi bu sağlayıcılara aktarılmaz; şifreli olarak yalnızca
Katip'in veritabanında tutulur.</p>
<h2>5. Saklama süresi</h2><p>Kayıtlar {retention} boyunca saklanır, ardından silinir ya da
anonim hâle getirilir. Ses izi, rıza geri alındığında ("Katip, beni unut") derhal silinir.</p>
<h2>6. Haklarınız</h2><p>KVKK md. 11 uyarınca kişisel verinizin işlenip işlenmediğini öğrenme,
bilgi talep etme, düzeltilmesini ya da silinmesini isteme, itiraz etme ve zararın giderilmesini
talep etme haklarına sahipsiniz. Toplantı sırasında "Katip, kayıt dışı" diyerek bir bölümün
kaydedilmemesini sağlayabilirsiniz. {_OBJECTION} Başvuru: {contact}.</p>
</body></html>"""
