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
        logger.info(f"WebSocket disconnected with code: {close_code}")
        await self.cleanup_process()

    async def receive(self, text_data):
        logger.info(f"Received message: {text_data}")
        try:
            data = json.loads(text_data)
            if 'ping' in data:
                await self.send(text_data=json.dumps({'pong': True}))
                logger.info("Sent pong")
                return
            if 'code' in data:
                if self.is_running:
                    await self.send(text_data=json.dumps({'error': 'Another process is running, please wait'}))
                    logger.info("Rejected code: process already running")
                    return
                await self.cleanup_process()
                await self.execute_code(data['code'])
            elif 'input' in data:
                if self.is_running and self.pty_fd is not None:
                    await self.input_queue.put(data['input'] + '\n')
                    logger.info(f"Queued input: {data['input']}")
                else:
                    logger.warning("Received input but no active process")
                    await self.send(text_data=json.dumps({'error': 'No active process'}))
            else:
                await self.send(text_data=json.dumps({'error': 'Invalid message'}))
                logger.warning("Received invalid message")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Invalid JSON: {str(e)}'}))
            await self.close(code=3000)

    async def execute_code(self, code):
        logger.info(f"Executing code: {code}")
        self.current_code = code
        self.expecting_input = 'input(' in code.lower()
        self.input_count = code.lower().count('input(')
        self.processed_inputs = 0
        self.output_received = False
        temp_dir = '/tmp'
        try:
            with tempfile.NamedTemporaryFile(suffix='.py', dir=temp_dir, delete=False) as temp_file:
                temp_file.write(code.encode('utf-8'))
                temp_file.flush()
                self.temp_file_path = temp_file.name
                logger.info(f"Starting PTY subprocess for file: {self.temp_file_path}")
                self.pty_fd, child_fd = pty.openpty()
                self.process = await asyncio.create_subprocess_exec(
                    'python3', '-u', self.temp_file_path,
                    stdin=child_fd,
                    stdout=child_fd,
                    stderr=child_fd,
                    env={'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'},
                    close_fds=True
                )
                os.close(child_fd)
            self.is_running = True
            asyncio.create_task(self.stream_output())
            asyncio.create_task(self.process_input())
            asyncio.create_task(self.watchdog())
        except PermissionError as e:
            logger.error(f"Permission error: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Execution failed: {str(e)}'}))
            await self.cleanup_process()
        except Exception as e:
            logger.error(f"PTY execution error: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Execution failed: {str(e)}'}))
            await self.cleanup_process()

    async def stream_output(self):
        try:
            while self.is_running and self.process and self.process.returncode is None:
                try:
                    rlist, _, _ = select.select([self.pty_fd], [], [], 30)
                    if self.pty_fd in rlist:
                        try:
                            output = os.read(self.pty_fd, 1024).decode('utf-8', errors='replace').strip()
                            if output:
                                self.output_received = True
                                formatted_output = output
                                if self.expecting_input and self.processed_inputs < self.input_count:
                                    formatted_output = output + ' >>>'
                                logger.info(f"Sending output: {formatted_output}")
                                for attempt in range(3):  # Retry sending up to 3 times
                                    try:
                                        await self.send(text_data=json.dumps({'output': formatted_output + '\n'}))
                                        break
                                    except Exception as send_err:
                                        logger.warning(f"Send attempt {attempt + 1} failed: {str(send_err)}")
                                        if attempt == 2:
                                            raise send_err
                                        await asyncio.sleep(0.1)
                        except OSError as e:
                            if e.errno == 5 and self.output_received and not self.expecting_input:
                                logger.info("Ignoring I/O error after successful output for non-input code")
                                break
                            raise
                    else:
                        logger.warning("No output received, continuing to wait")
                except Exception as e:
                    logger.error(f"Stream error: {str(e)}")
                    if not (self.output_received and not self.expecting_input):
                        await self.send(text_data=json.dumps({'error': f'Stream error: {str(e)}'}))
                    break
            if self.process and self.process.returncode is not None:
                logger.info(f"Process exited with returncode: {self.process.returncode}")
                if self.output_received or self.expecting_input:
                    await self.send(text_data=json.dumps({'output': f'\nProcess exited with code {self.process.returncode}\n'}))
        finally:
            await self.cleanup_process()

    async def process_input(self):
        try:
            while self.is_running and self.process and self.process.returncode is None:
                input_data = await self.input_queue.get()
                if self.pty_fd is not None:
                    try:
                        os.write(self.pty_fd, input_data.encode('utf-8'))
                        logger.info(f"Sent input: {input_data.strip()}")
                        self.processed_inputs += 1
                        self.expecting_input = self.processed_inputs < self.input_count
                    except OSError as e:
                        logger.error(f"Input write error: {str(e)}")
                        await self.send(text_data=json.dumps({'error': f'Input error: {str(e)}'}))
                        break
                self.input_queue.task_done()
            if self.process and self.process.returncode is not None:
                logger.info(f"Process exited with returncode: {self.process.returncode}")
        except Exception as e:
            logger.error(f"Input error: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Input error: {str(e)}'}))
        finally:
            await self.cleanup_process()

    async def watchdog(self):
        await asyncio.sleep(30)  # Reduced to 30 seconds
        if self.is_running and self.process and self.process.returncode is None and not self.output_received:
            logger.warning("Watchdog: Process stalled, terminating")
            await self.send(text_data=json.dumps({'error': 'Process stalled, terminated'}))
            await self.cleanup_process()

    async def cleanup_process(self):
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=2)
                    logger.info(f"Terminated process, returncode: {self.process.returncode}")
                except asyncio.TimeoutError:
                    logger.warning("Process did not terminate gracefully, killing")
                    self.process.kill()
                    await self.process.wait()
            except Exception as e:
                logger.error(f"Cleanup error: {str(e)}")
        if self.pty_fd is not None:
            try:
                os.close(self.pty_fd)
                logger.info("Closed PTY FD")
            except Exception as e:
                logger.error(f"Failed to close PTY FD: {str(e)}")
        self.process = None
        self.pty_fd = None
        self.is_running = False
        self.current_code = None
        self.expecting_input = False
        self.input_count = 0
        self.processed_inputs = 0
        self.output_received = False
        if self.temp_file_path and os.path.exists(self.temp_file_path):
            try:
                os.unlink(self.temp_file_path)
                logger.info(f"Deleted temp file: {self.temp_file_path}")
            except Exception as e:
                logger.error(f"Failed to delete temp file {self.temp_file_path}: {str(e)}")
        self.temp_file_path = None
        while not self.input_queue.empty():
            try:
                self.input_queue.get_nowait()
                self.input_queue.task_done()
            except asyncio.QueueEmpty:
                break
        logger.info("Cleanup completed")
