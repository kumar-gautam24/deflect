"""Token cost accounting and trace persistence."""



# USD per million tokens, input and output. Unpriced models cost nothing to record.
PRICING: dict[str, tuple[float, float]] = {
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.0-pro": (1.25, 5.00),
    # Groq's paid per-million rates. Not zeroes: the free tier costs nothing today, but
    # a 0.0 here would make every trace claim the answer was free.
    "openai/gpt-oss-120b": (0.15, 0.75),
    "openai/gpt-oss-20b": (0.10, 0.50),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    if model not in PRICING:
        return 0.0
    input_price, output_price = PRICING[model]
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000
