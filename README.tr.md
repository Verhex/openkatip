# Katip · openkatip

Katip, kendi sunucunuzda çalıştırabileceğiniz iki bağımsız Python motorundan oluşan
bir yapay zekâ asistanıdır. Yönetim katmanı ve saklanan veriler sizin sunucunuzdadır;
seçtiğiniz harici konuşma ve dil sağlayıcıları kendilerine gönderilen veriyi işler.

| Ürün | Kapsam |
| --- | --- |
| **Katip Meet** | Microsoft Teams, Google Meet ve Zoom toplantı botu; Türkçe komutlar, transkript, çeviri, notlar ve raporlar. |
| **Katip Ops** | Slack üzerinden operasyon işleri, Odoo işlemleri, politika denetimi, sahip onayı ve işlem defteri. |
| **Katip Full** | Aynı repodaki iki motorun, ayrı sırlar ve güvenlik sınırlarıyla birlikte kurulması. |

Full bir kurulum seçimidir. Meet ile Ops arasında otomatik iş başlatma yoktur;
[olay sözleşmesi](engines/ops/EVENTS.md) gelecekteki entegrasyonun tanımıdır.

## Özellikler

- Canlı altyazı ve çeviri; yazılı veya sesli yanıt; özet, karar ve aksiyon kaydı.
- Hazırlık belgeleri; ayrıca seçilerek yüklenen PDF sunumları ve onaylı sunum başlangıcı.
- PDF/Word raporları, toplantıya göre erişim, takvim katılım politikası,
  Microsoft 365 bağlantısı ve onaylı toplantı notu e-postası.
- Türkçe panel, toplantı başına tarayıcı işçisi, yalıtılmış belge ayrıştırıcı,
  yapılandırıldığında toplantı içeriği şifrelemesi ve saklama süreleri.
- Ops içerik, satış fırsatı araştırması, GEO ölçümü ve cuma değerlendirmesi;
  Claude/OpenAI/DeepSeek yönlendirmesi ve yalnız yetkili sahibin verebildiği onaylar.

Toplantı platformlarının arayüzleri değişebilir. Lobi, misafir, altyazı ve ekran
paylaşımı izinleri toplantı düzenleyicisine ve platforma bağlıdır.

## Hızlı başlangıç: tek Linux sunucusu

[Kurulum rehberinde](docs/getting-started.md) anlatıldığı gibi `.env` dosyasını,
sır dosyalarını, Docker soketi grubunu ve HTTPS erişimini hazırlayın:

```sh
cp .env.example .env
# Önce zorunlu kurulum alanlarını ve sır dosyalarını tamamlayın.
docker compose -f docker-compose.isolated.yml --profile build build
docker compose -f docker-compose.isolated.yml up -d
```

API yalnız sunucunun `127.0.0.1:8000` adresinde yayınlanır. Kendi HTTPS ters
vekiliniz veya kimlik doğrulamalı tüneliniz üzerinden `/admin` adresine girin.
Bu dosya Meet, işçi yöneticisi, ayrıştırıcı ve PostgreSQL'i kurar.
[Ops ayrı kurulur](docs/ops-engine.md).

## Mimari

Panel → Meet API → veritabanı/dosyalar ve yalıtılmış ayrıştırıcı.
İşçi yöneticisi → toplantı başına Chromium → toplantı platformu ve API.
API → yapılandırılmış STT/TTS/LLM sağlayıcıları.
Slack → Ops işleri/politika/onay → işlem defteri ve Odoo JSON-2 API.

Docker soketi yalnız yöneticidedir; toplantı işçileri sunucu sağlayıcı anahtarlarını
almaz. [Mimari](docs/architecture.md) ve [üretim kurulumu](docs/deployment.md).

## Ekran görüntüleri

TODO: herkese açık sürüm etiketi öncesinde yönetim paneli, canlı panel ve Ops onay
kartının incelenmiş, anonimleştirilmiş görüntülerini ekleyin.

## Yapılandırma, güvenlik ve kişisel veriler

[Yapılandırma rehberi](docs/configuration.md) tüm Meet ortam değişkenlerini;
[sağlayıcı rehberi](docs/providers.md) işleme yollarını ve bölgeleri açıklar.
Panel ve takvim güvenilir alan adı listeleri varsayılan olarak boştur.
Kurum bilgileri, sırlar ve işletim belgeleri kuruluma özel tutulmalıdır.

Panel parolaları scrypt ile özetlenir; güvenli oturum çerezleri ve CSRF denetimi
kullanılır. İmzalı rapor/panel bağlantıları erişim yetkisi taşır; bağlantıyı bilen
kişi geçerli kapsamda erişebilir. [Güvenlik modeli](docs/security.md).

Katip girişte Türkçe aydınlatma metni paylaşır. Normal toplantı oturumu ham sesi
saklamaz; transkriptler, çeviriler, belgeler ve istenen ekran görüntüleri kişisel veri
içerebilir. Kendi sunucunuzda çalıştırmak, yapay zekâ işlemlerinin yerel yapıldığı
veya KVKK/GDPR uyumunun otomatik sağlandığı anlamına gelmez. İşletmeci hukuki sebebi,
aktarımları, aydınlatmayı, saklama süresini ve hak başvurularını yönetmelidir.
Toplantı oturumunda biyometrik tanıma kapalıdır; eski ses ayarları uyumluluk içindir.

## Belgeler ve katkı

- [English README](README.md) · [Türkçe kullanıcı rehberi](docs/meeting-agent.md)
- [Yönetici rehberi](docs/admin-guide.md) · [Ops](docs/ops-engine.md)
- [Geliştirici rehberi](docs/developer-guide.md) · [SSS](docs/faq.md)
- [Katkı](CONTRIBUTING.md) · [Davranış kuralları](CODE_OF_CONDUCT.md)
- [Güvenlik açığı bildirimi](SECURITY.md)

## Lisans ve marka

Kod ve belgeler [MIT Lisansı](LICENSE) altındadır: Copyright (c) 2026 Verhex.
Bağımlılıkların kendi lisansları geçerlidir; [NOTICE](NOTICE).
**Katip adı ve logosu MIT kapsamında lisanslanmaz.** MIT marka hakkı veya destek
izlenimi verme izni sağlamaz. TODO: marka iletişim adresini ve kullanım politikasını yayımlayın.
