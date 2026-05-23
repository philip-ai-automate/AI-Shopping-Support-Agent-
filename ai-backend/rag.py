def build_prompt(system_prompt, context, user_message):
    return [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": f"Use the following context to answer:\n{context}"},
        {"role": "user", "content": user_message}
    ]
