def get_ai_prompt(categories):
    
    category_hints = ""
    for key, value in categories.items():
        category_hints += f"- {key}: {value["description"]}\n"
    
    return f"""
You are a professional document classifier.

OCR data will be provided as a text string. Your role is to interpret this content to distinguish between document types. The document must be categorized into exacly one of: {", ".join(categories.keys())}.

For your reference, here is a general description of each category:
{category_hints}

Responses MUST be valid JSON matching the schema. No markdown or extra commentary.
    """
