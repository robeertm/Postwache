#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Die erfundene Welt der Postwache-Demo — EINMAL, fuer alle, die sie brauchen.

Stand bis 05.10.2026 in `demo_daten.py`. Herausgeloest, weil `demo_imap.py`
denselben Korpus braucht: zwei Korpora waeren zwei Haushalte, und ein Absender,
der in der Liste anders heisst als in der Mail, faellt genau im Bild auf.

🔴 `demo_daten.py` liest beim Import die Befehlszeile (argparse auf Modulebene).
   Es laesst sich darum nicht importieren — daher diese eigene Datei und kein
   `from demo_daten import WELTEN`.
"""

# ── the invented world, twice ────────────────────────────────
# The same household, the same numbers — only in the respective language. The
# drawer keys (frist, amt, …) stay the same: the page translates those.
WELTEN = {}

WELTEN["de"] = dict(
    absender=[
        # (Adresse, Name, Klasse, Ordner, wie oft)
        ("rechnung@stadtwerke-elmbruck.example", "Stadtwerke Elmbruck", "frist", "Rechnungen", 24),
        ("service@nordlicht-versicherung.example", "Nordlicht Versicherung", "amt", "Versicherung", 11),
        ("post@kreissparkasse-elmbruck.example", "Kreissparkasse Elmbruck", "amt", "Bank", 18),
        ("buergerbuero@elmbruck.example", "Stadt Elmbruck", "amt", "Behoerden", 6),
        ("versand@paket24.example", "Paket24", "automatisch", "Shopping.Paket24", 63),
        ("newsletter@gartenhaus-weber.example", "Gartenhaus Weber", "newsletter", "Newsletter", 41),
        ("angebote@elektro-lindner.example", "Elektro Lindner", "werbung", "Newsletter", 37),
        ("sekretariat@schule-am-anger.example", "Schule Am Anger", "mensch", "Schule", 9),
        ("nas01@heimnetz.example", "NAS01", "automatisch", "Technik.Sicherungen", 88),
        ("nas02@heimnetz.example", "NAS02", "automatisch", "Technik.Sicherungen", 86),
        ("no-reply@quellwerk.example", "Quellwerk", "unklar", "", 14),
        ("hinweis@leitungsdienst.example", "Leitungsdienst", "unklar", "", 8),
        ("m.kessler@dachdecker-kessler.example", "Martin Kessler", "mensch", "Handwerker", 5),
        ("sicherheit@kontoschutz.example", "Kontoschutz", "sicherheit", "", 3),
    ],
    betreff={
        "frist": ["Ihre Abschlagsrechnung {m} 2026", "Jahresabrechnung Strom 2025",
                  "Zahlungserinnerung — Vertrag 44-20871"],
        "amt": ["Beitragsanpassung zum 01.01.2027", "Ihr Kontoauszug {m} 2026",
                "Bescheid ueber Grundsteuer 2026", "Neue Nachricht im Postfach"],
        "automatisch": ["[heimnetz]Sicherung abgeschlossen", "Ihre Sendung ist unterwegs",
                        "Zustellung fuer heute angekuendigt", "[heimnetz]Pruefung ohne Befund"],
        "newsletter": ["Herbst im Garten — 12 Ideen", "Unser Newsletter {m}",
                       "Neu eingetroffen: Hochbeete"],
        "werbung": ["-20 % auf alles bis Sonntag", "Nur heute: Aktionswoche"],
        "mensch": ["Elternabend am 8. Oktober", "Kurze Rueckfrage zum Angebot",
                   "Termin naechste Woche?"],
        "unklar": ["Ihre Anfrage 2026-{n}", "Wichtige Information", "Statusmeldung {n}"],
        "sicherheit": ["Ungewoehnliche Anmeldung bemerkt", "Bitte bestaetigen Sie Ihr Konto"],
    },
    monate=["Januar", "Februar", "Maerz", "April", "Mai", "Juni", "Juli",
            "August", "September", "Oktober", "November", "Dezember"],
    dokumente=[("Abschlagsrechnung-09-2026.pdf", "Stadtwerke Elmbruck", "Rechnungen", 148230),
               ("Beitragsanpassung-2027.pdf", "Nordlicht Versicherung", "Versicherung", 203440),
               ("Kontoauszug-2026-08.pdf", "Kreissparkasse Elmbruck", "Bank", 96110),
               ("Grundsteuerbescheid-2026.pdf", "Stadt Elmbruck", "Behoerden", 312880),
               ("Elternabend-Einladung.pdf", "Schule Am Anger", "Schule", 88420)],
    chronik=[
        ("sortiert", "Aussortiert", "18 Mail(s) aus dem Posteingang in Unterordner verschoben."),
        ("dokumente", "Dokumente an DocuSort", "3 Dokument(e) aus neuer Post uebergeben."),
        ("vorschlaege", "2 Regelvorschlaege",
         "Aus 9 unklaren Mails. Auf der Seite ansehen und einzeln uebernehmen."),
        ("sortiert", "Aussortiert", "23 Mail(s) aus dem Posteingang in Unterordner verschoben."),
        ("kaltstart", "Kaltstart",
         "300 Mails als Ausgangslage eingeordnet — nichts gemeldet, nichts verschoben."),
    ],
    postfaecher=[("privat", "Privat", "post@beispielhaus.example", True),
                 ("buero", "Buero", "buero@beispielhaus.example", False)],
    vorschlaege=[
        ("no-reply@quellwerk.example", "automatisch",
         "Immer derselbe Absender, immer dieselbe Betreffform — Maschinenpost.", 84),
        ("hinweis@leitungsdienst.example", "newsletter",
         "Rundschreiben mit Abmeldelink, kein persoenlicher Bezug.", 71)],
)

WELTEN["en"] = dict(
    absender=[
        ("billing@elmbrook-utilities.example", "Elmbrook Utilities", "frist", "Invoices", 24),
        ("service@northlight-insurance.example", "Northlight Insurance", "amt", "Insurance", 11),
        ("post@elmbrook-savings.example", "Elmbrook Savings Bank", "amt", "Bank", 18),
        ("cityhall@elmbrook.example", "City of Elmbrook", "amt", "Authorities", 6),
        ("shipping@parcel24.example", "Parcel24", "automatisch", "Shopping.Parcel24", 63),
        ("newsletter@weber-gardens.example", "Weber Gardens", "newsletter", "Newsletter", 41),
        ("offers@lindner-electric.example", "Lindner Electric", "werbung", "Newsletter", 37),
        ("office@anger-lane-school.example", "Anger Lane School", "mensch", "School", 9),
        ("nas01@homenet.example", "NAS01", "automatisch", "Tech.Backups", 88),
        ("nas02@homenet.example", "NAS02", "automatisch", "Tech.Backups", 86),
        ("no-reply@springworks.example", "Springworks", "unklar", "", 14),
        ("notice@pipeline-services.example", "Pipeline Services", "unklar", "", 8),
        ("m.kessler@kessler-roofing.example", "Martin Kessler", "mensch", "Trades", 5),
        ("security@account-guard.example", "Account Guard", "sicherheit", "", 3),
    ],
    betreff={
        "frist": ["Your instalment invoice, {m} 2026", "Annual electricity statement 2025",
                  "Payment reminder — contract 44-20871"],
        "amt": ["Premium adjustment as of 1 January 2027", "Your account statement, {m} 2026",
                "Property tax assessment 2026", "New message in your mailbox"],
        "automatisch": ["[homenet] Backup completed", "Your parcel is on its way",
                        "Delivery announced for today", "[homenet] Check completed, nothing found"],
        "newsletter": ["Autumn in the garden — 12 ideas", "Our newsletter, {m}",
                       "Just arrived: raised beds"],
        "werbung": ["-20 % on everything until Sunday", "Today only: promotion week"],
        "mensch": ["Parents' evening on 8 October", "Quick question about your quote",
                   "Time for a call next week?"],
        "unklar": ["Your enquiry 2026-{n}", "Important information", "Status notice {n}"],
        "sicherheit": ["Unusual sign-in noticed", "Please confirm your account"],
    },
    monate=["January", "February", "March", "April", "May", "June", "July",
            "August", "September", "October", "November", "December"],
    dokumente=[("Instalment-invoice-09-2026.pdf", "Elmbrook Utilities", "Invoices", 148230),
               ("Premium-adjustment-2027.pdf", "Northlight Insurance", "Insurance", 203440),
               ("Account-statement-2026-08.pdf", "Elmbrook Savings Bank", "Bank", 96110),
               ("Property-tax-assessment-2026.pdf", "City of Elmbrook", "Authorities", 312880),
               ("Parents-evening-invitation.pdf", "Anger Lane School", "School", 88420)],
    chronik=[
        ("sortiert", "Sorted out", "Moved 18 mail(s) from the inbox into subfolders."),
        ("dokumente", "Documents to DocuSort", "Handed over 3 document(s) from new mail."),
        ("vorschlaege", "2 rule suggestions",
         "From 9 unclear mails. Look at them on the page and adopt them one by one."),
        ("sortiert", "Sorted out", "Moved 23 mail(s) from the inbox into subfolders."),
        ("kaltstart", "Cold start",
         "Filed 300 mails as a baseline — nothing reported, nothing moved."),
    ],
    postfaecher=[("privat", "Private", "post@examplehouse.example", True),
                 ("buero", "Office", "office@examplehouse.example", False)],
    vorschlaege=[
        ("no-reply@springworks.example", "automatisch",
         "Always the same sender, always the same shape of subject — machine mail.", 84),
        ("notice@pipeline-services.example", "newsletter",
         "A circular with an unsubscribe link, nothing personal in it.", 71)],
)
