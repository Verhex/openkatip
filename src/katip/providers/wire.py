"""Gönderici hatasını bekleyen WebSocket alıcısına da taşır."""

import asyncio

from katip.async_utils import wait_tasks


async def messages(ws, pump, *background):
    incoming = ws.__aiter__()
    while True:
        receive = asyncio.create_task(anext(incoming))
        try:
            done, _ = await asyncio.wait(
                (receive, pump, *background), return_when=asyncio.FIRST_COMPLETED
            )
            for task in background:
                if task in done:
                    task.result()
            if pump in done:
                pump.result()
                # Ses bitti: sunucunun kalan kesinleşmiş metni için sınırlı bekle. Bu sürede
                # hiçbir şey gelmezse akış normal biter; kopma sayılıp yeniden başlatılmaz.
                try:
                    async with asyncio.timeout(10):
                        result = await receive
                except TimeoutError:
                    return
                yield result
            else:
                yield receive.result()
        except StopAsyncIteration:
            if not pump.done():
                raise ConnectionError("Ses bitmeden STT bağlantısı kapandı") from None
            pump.result()
            return
        finally:
            await wait_tasks({receive}, budget_s=1, label="STT alıcısı", cancel=True)
