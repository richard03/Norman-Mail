# Bezpečnostní issues

Nálezy z bezpečnostních auditů (7. 11. a 16. 11. 2026), seřazené od nejzávažnějších. Řešíme je jeden po druhém;
vyřešené issue se ze souboru smaže.

---

## 14. Našeptávání adres lze ovlivnit cizím mailem (nízká)

**Problém:** Kontakty se učí i z přijaté pošty od lidí, kterým jste nikdy nepsali (s nízkou váhou).
Útočník pošle mail se jménem „Jan Novák“ ze své adresy a při psaní „Jan“ se jeho adresa nabídne vedle
skutečné. Adresa je v našeptávači vidět, takže riziko je malé, ale překlep v rychlosti je snadný.

**Kde:** `db.py` – `_learn_contacts`, `contacts()`.

**Oprava:** Z přijaté pošty se učit jen od odesílatelů, kterým jste už někdy psali, nebo jen ze zpráv, kde
`Authentication-Results` hlásí `dmarc=pass` (souvisí s issue 5). Případně kontakty jen z přijaté pošty
v našeptávači vizuálně odlišit.

---

