# Ověření aplikace u Googlu

Cíl: uživatel klikne na **Přihlásit přes Google**, přihlásí se a povolí přístup. Nic nenastavuje, nevidí varování „Google tuto aplikaci neověřil“ a neplatí žádný limit 100 uživatelů.

Aplikace žádá scope `https://mail.google.com/`. Google ho řadí mezi **restricted** scopes, takže ověření má dvě části:

1. **Kontrola značky a scopes** (brand verification a restricted scope review). Kontroluje se web, zásady ochrany soukromí, video a zdůvodnění scopes.
2. **Bezpečnostní audit CASA** (Cloud Application Security Assessment, úroveň Tier 2). Dělá ho externí laboratoř schválená App Defense Alliance a platí se jí. Opakuje se každých 12 měsíců.

## Co je v repozitáři hotové

| Požadavek Googlu | Kde |
|---|---|
| Domovská stránka, která popisuje aplikaci | `site/index.html` |
| Zásady ochrany soukromí včetně prohlášení o Limited Use | `site/privacy.html` (oddíl 5) |
| Podmínky použití | `site/terms.html` |
| Odkaz na zásady v aplikaci | Dialog účtu pod tlačítkem Google, Nastavení → O aplikaci |
| Tlačítko podle pravidel značky Google | `static/style.css` (`.btn.google`), text „Sign in with Google“, standardní logo |
| Logo 120 × 120 px | `assets/mailer-120.png` |
| Vestavěný OAuth klient (uživatel nic nezadává) | `google_client.py`, `NormanMail.spec`, `tools/release.py` |
| Bezpečné přihlášení | OAuth pro desktop s PKCE (S256), loopback na `127.0.0.1`, kontrola `state`, tokeny ve Správci pověření Windows |

## Postup

### 1. Web na vlastní ověřené doméně

Google ověřuje vlastnictví domény domovské stránky v [Google Search Console](https://search.google.com/search-console). Musí to být stejný účet Google, který vlastní projekt v Cloud Console.

- **GitHub Pages** (`https://richard03.github.io/Norman-Mail/`): workflow `.github/workflows/pages.yml` vystaví složku `site/`. Jednorázově: repozitář → Settings → Pages → Source: **GitHub Actions**. Jenže `github.io` je veřejná přípona, takže se ověřuje doména `richard03.github.io`. Ověřovací soubor proto musí ležet v jejím kořeni, tedy v samostatném repozitáři `richard03.github.io`. Projekt na podadrese `/Norman-Mail/` sám ověřit nejde.
- **Vlastní doména (doporučuju):** např. `normanmail.cz`, ověření záznamem DNS TXT je jednoduché a spolehlivé. Na GitHub Pages ji nastavíte v Settings → Pages → Custom domain. Pak změňte adresy `https://richard03.github.io/Norman-Mail/` v `static/index.html` (odkazy na zásady a podmínky) a v komentáři `pages.yml`.

### 2. Google Cloud Console → Google Auth Platform

Použijte projekt, ve kterém už máte klienta, nebo založte nový. Doporučuju samostatný projekt jen pro vydávanou aplikaci.

- **Branding:** App name `Norman Mail` (stejný název jako na webu i v aplikaci), User support email, logo `assets/mailer-120.png`, Application home page, Privacy policy link (`…/privacy.html`), Terms of service link (`…/terms.html`), Authorized domains (doména z kroku 1), Developer contact.
- **Audience:** User type **External**, stav **In production** (Publish app).
- **Data access:** přidejte scopes `openid`, `.../auth/userinfo.email` (nejsou citlivé) a `https://mail.google.com/` (restricted). Žádné další, Google chce co nejmenší rozsah.
- **Clients:** OAuth client typu **Desktop app**. Stáhněte JSON a uložte ho jako `%USERPROFILE%\.normanmail\google_client.json`. Odtud ho sestavení přibalí do exe.
- **APIs & Services → Library:** zapněte **Gmail API**.

Na vlastním počítači máte `%APPDATA%\Mailer\google_client.json` z dřívějšího testování. Ten má přednost před vestavěným klientem. Chcete-li vyzkoušet vestavěného klienta, soubor smažte a účty Gmail přihlaste znovu.

### 3. Zdůvodnění scope (do formuláře, anglicky)

> Norman Mail is a desktop email client for Windows (IMAP/SMTP). It requests `https://mail.google.com/` because Gmail's IMAP and SMTP servers accept XOAUTH2 authentication only with this scope; narrower Gmail API scopes (such as `gmail.modify` or `gmail.readonly`) do not work with IMAP/SMTP. The scope is used solely for user-facing email client features: downloading and displaying the user's messages and folders, searching, sorting messages into views, moving, archiving, flagging and deleting messages at the user's request, and sending messages the user writes (with a copy saved to Sent). All data is processed and stored only on the user's own computer. Norman Mail has no servers; no data is transmitted to the developer or any third party, and it is not used for advertising or to train AI/ML models. The `openid` and `email` scopes are used only to identify which Gmail address the user signed in with.

### 4. Ukázkové video (YouTube, neveřejné, anglicky nebo s anglickými titulky)

Google chce vidět celé přihlášení a použití každého scope:

1. Spuštění aplikace, **+ Add account → Gmail → Sign in with Google**.
2. Okno souhlasu Googlu: musí být čitelný **název aplikace** a v adresním řádku **Client ID** (`client_id=…`). Zvětšete adresní řádek a ukažte ho.
3. Seznam požadovaných oprávnění, povolení a návrat do aplikace.
4. Použití scope: zobrazení složek a zpráv, přečtení zprávy, hledání, přesun do složky nebo archivu, smazání, označení hvězdičkou, napsání a odeslání zprávy (a její kopie v Odeslaných).
5. Odkaz na zásady ochrany soukromí v aplikaci (dialog účtu, Nastavení → O aplikaci).
6. Odebrání účtu a zmínka o zrušení přístupu na myaccount.google.com/permissions.

### 5. Bezpečnostní audit CASA

Po schválení scopes pošle Google e-mail s pokyny. Audit (Tier 2) provede laboratoř ze seznamu App Defense Alliance a vy jí zaplatíte. Ceny se liší podle laboratoře, počítejte se stovkami dolarů a výš, a to každý rok. Laboratoř zpravidla chce výsledky automatického skenu kódu (SAST/DAST) a vyplněný dotazník. Možné otázky a odpovědi:

- **Kde jsou tokeny:** obnovovací token ve Správci pověření Windows (`keyring`), přístupové tokeny jen v paměti.
- **Šifrování přenosu:** IMAP a SMTP přes TLS (`imap.gmail.com:993`, `smtp.gmail.com:465`), OAuth přes HTTPS.
- **Data v klidu:** kopie zpráv jsou v lokální databázi SQLite v datové složce uživatele (`%APPDATA%\Mailer`). Ta je chráněná oprávněními profilu Windows, ale šifrovaná není. Na tohle se laboratoř může ptát.
- **Lokální server:** jen během přihlášení, na náhodném portu `127.0.0.1`, přijímá jen `GET /oauth/callback` s platným `state`.
- **Aktualizace:** podepsané Ed25519, ověřené před instalací.
- **Smazání dat:** odebrání účtu smaže zprávy i token, celá data se smažou odstraněním datové složky.

### 6. Po schválení

Vydejte novou verzi přes `tools/release.py`. Vydání bez souboru s klientem skript odmítne. Uživatelé starších verzí, kteří si zadali vlastního klienta, ho dál používají. Kdo chce přejít na vestavěného klienta, smaže `%APPDATA%\Mailer\google_client.json` a účet Gmail přihlásí znovu.
