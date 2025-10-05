@dp.message()
async def chat_with_assistant(msg: types.Message):
    user_input = msg.text.strip()

    # Получаем имя пользователя из Telegram
    first_name = msg.from_user.first_name or "друг"
    last_name = msg.from_user.last_name or ""
    full_name = f"{first_name} {last_name}".strip()

    try:
        # Создаём поток (thread) с персонализацией
        thread = await client.beta.threads.create_and_run(
            assistant_id=GPT_ASSISTANT_ID,
            thread={
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"Ты — AI-консультант VPN GPT. "
                            f"Ты общаешься с пользователем по имени {full_name}. "
                            f"Будь дружелюбен и персонализируй ответы. "
                            f"Если знаешь имя, используй его естественно, но не в каждом сообщении. "
                            f"Веди себя как живой эксперт, помогающий выбрать и настроить VPN."
                        )
                    },
                    {"role": "user", "content": user_input},
                ]
            }
        )

        # Получаем ответ
        messages = await client.beta.threads.messages.list(thread_id=thread.id)
        if messages.data:
            reply = messages.data[0].content[0].text.value
            await msg.answer(reply)
        else:
            await msg.answer("⚠️ GPT не прислал ответ. Попробуй позже.")

    except Exception as e:
        print("Error communicating with GPT Assistant:", e)
        await msg.answer("⚠️ Произошла ошибка при обращении к GPT. Попробуй позже.")

