import os

def generate_ai_analysis(exception_str: str) -> str | None:
    ai_platform = os.getenv("AI_PLATFORM", "").lower()

    if ai_platform == "openai":
        import openai
        client = openai.OpenAI()
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o"),
            messages=[
                {"role": "system", "content": "You are a helpful expert assistant that analyzes exceptions and provides a detailed analysis and solution of the issue. Always respond in markdown format."},
                {"role": "user", "content": f"Analyze the following exception and provide a detailed analysis of the issue: {exception_str}"}
            ]
        )
        print(f"AI Response generated")
        return response.choices[0].message.content

    elif ai_platform == "anthropic":
        import anthropic
        client = anthropic.Anthropic()
        message = client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"),
            system="You are a helpful expert assistant that analyzes exceptions and provides a detailed analysis and solution of the issue. Always respond in markdown format.",
            messages=[
                {"role": "user", "content": f"Analyze the following exception and provide a detailed analysis of the issue: {exception_str}"}
            ],
            max_tokens=1500
        )
        print(f"AI Response generated: {message.content}")
        return message.content[0].text
    print(f"No AI Response generated")
    return None
