import os

MODEL = "claude-sonnet-5-5"  # agar error aaye to docs.claude.com par current model naam check karein

SYSTEM = (
    "Tum ek facility manager ke liye resource waste alerts samjhate ho (bijli aur paani). "
    "Jawab Roman Urdu (Urdu Latin script mein) mein likho, saada zubaan mein, 90 se 120 alfaaz. "
    "Sirf diye gaye numbers istemal karo, koi naya number ya wajah mat banao. "
    "Kabhi ye mat kaho ke waste ya leak confirm ho chuki hai: ye 'excess' hai jo inspection ke baad confirm hogi. "
    "Aakhir mein manager ke liye ek clear agla qadam batao."
)

WORD = {"electricity": "bijli", "water": "paani"}


def _facts(a):
    evidence = "\n".join(f"- {e}" for e in a["evidence"])
    return (
        f"Building: {a['building']}\n"
        f"Resource: {WORD.get(a['resource'], a['resource'])}\n"
        f"Masla: {a['title']}\n"
        f"Expected: {a['expected']} {a['unit']}, measured: {a['measured']} {a['unit']}\n"
        f"Raatein affected: {a['nights_affected']}, total excess: {a['total_excess']} {a['total_unit']}\n"
        f"Potential excess cost: Rs {a['potential_cost_rs']} ({a['tariff_text']})\n"
        f"Confidence: {a['confidence']} ({a['confidence_score']}/100), method: {a['method']}\n"
        f"Evidence:\n{evidence}\n"
        "Data simulated hai, asli building measurements nahi."
    )


def _template(a):
    word = WORD.get(a["resource"], a["resource"])
    return (
        f"{a['building']} mein pichhli {a['nights_affected']} raaton ke dauran {word} ka istemal "
        f"normal se kaafi zyada raha: {a['measured']} {a['unit']}, jabke {a['expected']} {a['unit']} "
        f"expected tha. Is se lagbhag {a['total_excess']:,} {a['total_unit']} excess bana, jis ki "
        f"potential cost Rs {a['potential_cost_rs']:,} hai. Detection method ({a['method']}) ke hisaab se "
        f"confidence {a['confidence']} hai. Ye abhi confirmed masla nahi hai. "
        "Agla qadam: inspection karwayein aur wajah confirm karein."
    )


def explain(alert):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return {"source": "template", "text": _template(alert)}
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=MODEL,
            max_tokens=500,
            system=SYSTEM,
            messages=[{"role": "user", "content": "Is alert ko samjhao:\n\n" + _facts(alert)}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        if not text:
            raise ValueError("khali jawab")
        return {"source": "claude", "text": text}
    except Exception as e:
        return {"source": "template", "text": _template(alert), "error": str(e)[:150]}