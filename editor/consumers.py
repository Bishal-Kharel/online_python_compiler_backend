import json
import asyncio
import subprocess
import tempfile
import re
from channels.generic.websocket import AsyncWebsocketConsumer
import logging

logger = logging.getLogger(__name__)

class CodeConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.process = None
        self.input_queue = asyncio.Queue()
        self.is_running = False
        self.current_code = None
        await self.accept()
        logger.info("WebSocket connected")

    async def disconnect(self, close_code):
        logger.info(f"WebSocket disconnected with code: {close_code}")
        await self.cleanup_process()

    async def receive(self, text_data):
        logger.info(f"Received message: {text_data}")
        try:
            data = json.loads(text_data)
            if 'ping' in data:
                return
            if 'code' in data:
                if self.is_running:
                    await self.cleanup_process()
                await self.execute_code(data['code'])
            elif 'input' in data:
                if self.is_running and self.process:
                    await self.input_queue.put(data['input'])
                else:
                    await self.send(text_data=json.dumps({'error': 'No active process'}))
            else:
                await self.send(text_data=json.dumps({'error': 'Invalid message'}))
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Invalid JSON: {str(e)}'}))
            await self.close(code=3000)

    async def execute_code(self, code):
        logger.info("Executing code")
        self.current_code = code
        try:
            # Wrap code to force flush after input
            wrapper_code = (
                "import sys\n"
                "def flush_input(prompt=''):\n"
                "    result = input(prompt)\n"
                "    sys.stdout.flush()\n"
                "    return result\n"
                f"{code.replace('input(', 'flush_input(')}"
            )
            with tempfile.NamedTemporaryFile(suffix='.py', dir='/tmp', delete=False) as temp_file:
                temp_file.write(wrapper_code.encode())
                temp_file.flush()
                self.process = await asyncio.create_subprocess_exec(
                    'stdbuf', '-oL', 'python3', '-u', temp_file.name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE,
                    env={'PYTHONUNBUFFERED': '1'}
                )
            self.is_running = True
            asyncio.create_task(self.stream_output())
            asyncio.create_task(self.process_input())
        except Exception as e:
            logger.error(f"Execution error: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Execution failed: {str(e)}'}))
            await self.cleanup_process()

    async def stream_output(self):
        try:
            while self.is_running and self.process and self.process.returncode is None:
                stdout = await asyncio.wait_for(self.process.stdout.readline(), timeout=8)
                if not stdout:
                    continue
                output = stdout.decode().strip()
                if output:
                    formatted_output = output + ' >>>' if 'input' in self.current_code.lower() else output
                    logger.info(f"Sending output: {formatted_output}")
                    await self.send(text_data=json.dumps({'output': formatted_output + '\n'}))

                try:
                    stderr = await asyncio.wait_for(self.process.stderr.readline(), timeout=0.1)
                    if stderr:
                        error = stderr.decode().strip()
                        if error:
                            logger.info(f"Sending error: {error}")
                            await self.send(text_data=json.dumps({'output': error + '\n'}))
                except asyncio.TimeoutError:
                    pass
        except asyncio.TimeoutError:
            logger.info("Stream timeout, continuing")
        except Exception as e:
            logger.error(f"Stream error: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Stream error: {str(e)}'}))
        finally:
            await self.cleanup_process()

    async def process_input(self):
        try:
            while self.is_running and self.process and self.process.returncode is None:
                input_data = await self.input_queue.get()
                if self.process.stdin:
                    self.process.stdin.write((input_data + '\n').encode())
                    await self.process.stdin.drain()
                    logger.info(f"Sent input: {input_data}")
                self.input_queue.task_done()
        except Exception as e:
            logger.error(f"Input error: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Input error: {str(e)}'}))
        finally:
            await self.cleanup_process()

    async def cleanup_process(self):
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except Exception as e:
                logger.error(f"Cleanup error: {str(e)}")
        self.process = None
        self.is_running = False
        self.current_code = None
        logger.info("Cleanup completed")
