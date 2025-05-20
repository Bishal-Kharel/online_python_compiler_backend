import json
import asyncio
import pty
import os
import tempfile
import subprocess
import select
from channels.generic.websocket import AsyncWebsocketConsumer
import logging

logger = logging.getLogger(__name__)

class CodeConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.process = None
        self.input_queue = asyncio.Queue()
        self.is_running = False
        self.current_code = None
        self.expecting_input = False
        self.input_count = 0
        self.processed_inputs = 0
        self.temp_file_path = None
        self.pty_fd = None
        self.output_received = False
        await self.accept()
        logger.info("WebSocket connected")

    async def disconnect(self, close_code):
        logger.info(f"WebSocket disconnected: code={close_code}")
        await self.cleanup_process()

    async def receive(self, text_data):
        logger.info(f"WebSocket message received: {text_data}")
        try:
            data = json.loads(text_data)

            if 'ping' in data:
                await self.send_json({'pong': True})
                return

            if 'code' in data:
                if self.is_running:
                    await self.send_json({'error': 'Another process is running, please wait'})
                    return
                await self.cleanup_process()
                await self.execute_code(data['code'])

            elif 'input' in data:
                if self.is_running and self.pty_fd:
                    await self.input_queue.put(data['input'] + '\n')
                else:
                    await self.send_json({'error': 'No active process'})

            else:
                await self.send_json({'error': 'Invalid message'})

        except json.JSONDecodeError as e:
            logger.error(f"JSON error: {e}")
            await self.send_json({'error': f'Invalid JSON: {str(e)}'})
            await self.close(code=3000)

    async def execute_code(self, code):
        self.current_code = code
        self.expecting_input = 'input(' in code.lower()
        self.input_count = code.lower().count('input(')
        self.processed_inputs = 0
        self.output_received = False

        try:
            with tempfile.NamedTemporaryFile(suffix='.py', delete=False, dir='/tmp') as temp_file:
                temp_file.write(code.encode('utf-8'))
                self.temp_file_path = temp_file.name

            self.pty_fd, child_fd = pty.openpty()
            self.process = await asyncio.create_subprocess_exec(
                'python3', '-u', self.temp_file_path,
                stdin=child_fd, stdout=child_fd, stderr=child_fd,
                env={'PYTHONUNBUFFERED': '1'},
                close_fds=True
            )
            os.close(child_fd)
            self.is_running = True

            asyncio.create_task(self.stream_output())
            asyncio.create_task(self.process_input())
            asyncio.create_task(self.watchdog(timeout=30))

        except Exception as e:
            logger.error(f"Execution failed: {e}")
            await self.send_json({'error': f'Execution failed: {str(e)}'})
            await self.cleanup_process()

    async def stream_output(self):
        try:
            while self.is_running and self.process and self.process.returncode is None:
                rlist, _, _ = select.select([self.pty_fd], [], [], 30)
                if self.pty_fd in rlist:
                    try:
                        output = os.read(self.pty_fd, 1024).decode('utf-8', errors='replace').strip()
                        if output:
                            self.output_received = True
                            if self.expecting_input and self.processed_inputs < self.input_count:
                                output += ' >>>'
                            await self.send_json({'output': output + '\n'})
                    except OSError as e:
                        logger.warning(f"Output read error: {e}")
                        break
                else:
                    logger.info("Waiting for output...")

            if self.process and self.process.returncode is not None:
                await self.send_json({'output': f'\nProcess exited with code {self.process.returncode}\n'})

        except Exception as e:
            logger.error(f"Stream error: {e}")
            await self.send_json({'error': f'Stream error: {str(e)}'})
        finally:
            await self.cleanup_process()

    async def process_input(self):
        try:
            while self.is_running and self.process and self.process.returncode is None:
                input_data = await self.input_queue.get()
                if self.pty_fd:
                    try:
                        os.write(self.pty_fd, input_data.encode('utf-8'))
                        self.processed_inputs += 1
                        self.expecting_input = self.processed_inputs < self.input_count
                    except OSError as e:
                        await self.send_json({'error': f'Input error: {str(e)}'})
                        break
                self.input_queue.task_done()
        except Exception as e:
            logger.error(f"Input error: {e}")
            await self.send_json({'error': f'Input error: {str(e)}'})
        finally:
            await self.cleanup_process()

    async def watchdog(self, timeout=30):
        await asyncio.sleep(timeout)
        if self.is_running and self.process and self.process.returncode is None and not self.output_received:
            await self.send_json({'error': 'Process stalled, terminated'})
            await self.cleanup_process()

    async def cleanup_process(self):
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
            except Exception as e:
                logger.warning(f"Process cleanup error: {e}")

        if self.pty_fd:
            try:
                os.close(self.pty_fd)
            except Exception:
                pass

        if self.temp_file_path and os.path.exists(self.temp_file_path):
            try:
                os.remove(self.temp_file_path)
            except Exception as e:
                logger.warning(f"Temp file delete failed: {e}")

        self.process = None
        self.pty_fd = None
        self.is_running = False
        self.current_code = None
        self.expecting_input = False
        self.input_count = 0
        self.processed_inputs = 0
        self.output_received = False
        self.temp_file_path = None

        while not self.input_queue.empty():
            self.input_queue.get_nowait()
            self.input_queue.task_done()

        logger.info("Process cleaned up")

    async def send_json(self, content):
        try:
            await self.send(text_data=json.dumps(content))
        except Exception as e:
            logger.warning(f"Failed to send JSON: {e}")
