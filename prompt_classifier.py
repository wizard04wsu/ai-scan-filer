import json

def get_ai_prompt(categories):
    
    category_hints = ""
    for key, value in categories.items():
        category_hints += f"- {key}: {value}\n"
    
    return f"""
You are a professional document classifier.

OCR data will be provided in JSON format where 'x' and 'y' represent the spatial position of a word on the page. Your role is to interpret this layout to distinguish between document types. The document must be categorized into exacly one of: {", ".join(categories.keys())}.

For your reference, here is a general description of each category:
{category_hints}

Responses MUST be valid JSON matching the schema. No markdown or extra commentary.
    """
