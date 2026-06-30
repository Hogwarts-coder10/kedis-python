import asyncio
import time

from parser import KESPEncoder  # Reusing your KESP Exhaust!


async def fire_commands():
    reader, writer = await asyncio.open_connection("127.0.0.1", 6379)

    # 1. Test the Surge Tank with a massive 50KB payload
    massive_string = "V" * 50000
    payload = KESPEncoder.encode(["SET", "load_test", massive_string])
    writer.write(payload)
    await writer.drain()
    await reader.read(1024)
    print("✓ 50KB Surge Tank payload swallowed successfully.")

    # 2. Fire 10,000 rapid commands
    start = time.time()
    for i in range(10000):
        cmd = KESPEncoder.encode(["SET", f"key_{i}", str(i)])
        writer.write(cmd)
        await writer.drain()
        await reader.read(1024)

    end = time.time()
    print(f"✓ 10,000 KESP commands executed in {end - start:.2f} seconds.")

    writer.close()
    await writer.wait_closed()


asyncio.run(fire_commands())
