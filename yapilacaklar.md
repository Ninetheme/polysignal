# Yapilacaklar

## Sorun: Bot restart sonrasi pozisyonlari sifirliyor

Mevcut durumda bot yeniden baslatildiginda acik pozisyonlar korunmuyor.

Neden:
- Portfoy state'i sadece RAM'de tutuluyor.
- Bot her `_start()` cagrildiginda yeni bir `PaperPortfolio()` olusturuyor.
- `order_archive.json` ve `market_history.json` kalici, ama bunlardan aktif pozisyon geri yuklenmiyor.

Etkisi:
- Dashboard'da UP/DN pozisyonlari sifirlaniyor.
- Restart sonrasi strateji, onceki market veya onceki session envanterinden habersiz devam ediyor.
- Canli trade'e gecilirse bu davranis ciddi state drift riski yaratir.

## Cozum 1: Dosyadan portfoy snapshot geri yukleme

Yapilacak:
- Her fill sonrasi aktif portfoy snapshot'unu ayri bir JSON dosyasina yaz.
- Bot acilisinda bu snapshot'u okuyup `PaperPortfolio` state'ini geri yukle.
- Market degisimi ve manual reset durumlarinda snapshot'i kontrollu sifirla.

Artisi:
- Uygulamasi hizli.
- Test mode ve paper mode icin yeterince pratik.

Eksisi:
- Process crash, eksik yazim, bozuk dosya gibi durumlarda state drift olabilir.
- Gercek hesap/cuzdan ile kaynak dogruluk garanti edilmez.

## Cozum 2: Polymarket'ten gercek pozisyon reconcile etme

Yapilacak:
- Bot acilisinda Polymarket hesap/pozisyon endpointlerinden acik UP/DN inventory'yi cek.
- Local `PaperPortfolio` state'ini bu gercek veriye gore kur.
- Gerekirse local archive ile remote state arasinda reconcile/log mekanizmasi ekle.

Artisi:
- En dogru ve production'a uygun cozum.
- Restart, crash ve deploy sonrasi state kaybi problemini kokten cozer.

Eksisi:
- Uygulamasi daha zor.
- API entegrasyonu, auth ve reconcile mantigi dikkat ister.

## Oneri

Bu repo icin dogru uzun vadeli cozum `Cozum 2`.

Gecici ama hizli cozum gerekirse once `Cozum 1` uygulanip, ardindan `Cozum 2`'ye gecilebilir.
