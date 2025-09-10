async def send_long_message(channel, text: str, chunk_size: int = 1900) -> None:
    for i in range(0, len(text), chunk_size):
        await channel.send(text[i:i + chunk_size])
