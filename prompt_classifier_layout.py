def get_ai_prompt(categories):
    
    category_hints = ""
    for key, value in categories.items():
        category_hints += f"- {key}: {value['description']}\n"
    
    return f"""
You are a professional document classifier.

OCR data will be provided in JSON format where 'x' and 'y' represent the spatial position of a word on the page. Your role is to interpret this content to distinguish between document types. The document must be categorized into exacly one of: {", ".join(categories.keys())}.

For your reference, here is a general description of each category:
{category_hints}

Important category rules:
- "Financial_Account" is ONLY for bank/credit/investment/retirement statements and similar account documents.
- Confidence should rarely be 1.0; use 1.0 only when multiple strong signals match the category exactly.

Responses MUST be valid JSON matching the schema. No markdown or extra commentary.
    """
