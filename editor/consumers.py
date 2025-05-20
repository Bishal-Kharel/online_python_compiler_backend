import json
import asyncio
import subprocess
import tempfile
import os
import re
from channels.generic.websocket import AsyncWebsocketConsumer
import logging
import signal

logger = logging.getLogger(__name__)

class CodeConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        logger.info("WebSocket connection attempt")
        try:
            self.process = None
            self.temp_file_path = None
            self.input_queue = asyncio.Queue()
            self.is_running = False
            self.current_code = None
            self.input_count = 0
            self.input_processed = 0
            await self.accept()
            logger.info("WebSocket connected")
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            try:
                await self.accept()
                await self.send(text_data=json.dumps({'error': f'Connection failed: {str(e)}'}))
            except Exception as send_error:
                logger.error(f"Failed to send error message: {str(send_error)}")
            finally:
                await self.close(code=3000)

    async def disconnect(self, close_code):
        logger.info(f"WebSocket disconnected with code: {close_code}")
        await self.cleanup_process()
        self.is_running = False

    async def receive(self, text_data):
        logger.info(f"Received WebSocket message: {text_data}")
        try:
            data = json.loads(text_data)
            if 'code' in data:
                # Reuse process if same code and waiting for input
                if (self.current_code == data['code'] and self.is_running and 
                    self.process and self.input_queue.empty()):
                    logger.info("Same code received, continuing with existing process")
                    await self.send(text_data=json.dumps({'output': 'Waiting for input >>> '}))
                else:
                    if self.is_running:
                        await self.cleanup_process()
                    await self.execute_code(data['code'])
            elif 'input' in data:
                if self.is_running and self.process:
                    await self.input_queue.put(data['input'])
                    self.input_processed += 1
                else:
                    await self.send(text_data=json.dumps({'error': 'No active process for input'}))
            else:
                await self.send(text_data=json.dumps({'error': 'Invalid message format. Use {"code": "..."} or {"input": "..."}'}))
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Invalid JSON: {str(e)}'}))
            await self.close(code=3000)
        except Exception as e:
            logger.error(f"Unexpected error in receive: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Unexpected error: {str(e)}'}))

    async def execute_code(self, code):
        logger.info("Executing code")
        self.current_code = code
        self.input_count = len(re.findall(r'\binput\s*\(', code))
        self.input_processed = 0
        try:
            # Check memory usage to avoid OOM
            import psutil
            if psutil.virtual_memory().percent > 80:
                logger.error("Memory usage too high")
                await self.send(text_data=json.dumps({'error': 'Server resource limit reached'}))
                return

            # Write code to temporary file
            with tempfile.NamedTemporaryFile(suffix='.py', delete=False, dir='/tmp') as temp_file:
                temp_file.write(code.encode('utf-8'))
                self.temp_file_path = temp_file.name

            # Start subprocess
            self.process = await asyncio.create_subprocess_exec(
                'python3', '-u', self.temp_file_path,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={'PYTHONUNBUFFERED': '1'},
                preexec_fn=lambda: os.setpgrp()  # Run in new process group for cleanup
            )
            self.is_running = True
            logger.info("Process started")

            # Start tasks
            asyncio.create_task(self.stream_output())
            asyncio.create_task(self.process_input())
            asyncio.create_task(self.watchdog(timeout=60))

        except Exception as e:
            logger.error(f"Failed to start process or execution: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Execution failed: {str(e)}'}))
            await self.cleanup_process()

    async def stream_output(self):
        try:
            while self.is_running and self.process.returncode is None:
                try:
                    # Read stdout and stderr concurrently
                    stdout_task = asyncio.create_task(self.process.stdout.read(4096))
                    stderr_task = asyncio.create_task(self.process.stderr.read(4096))
                    done, pending = await asyncio.wait(
                        [stdout_task, stderr_task],
                        timeout=60,
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()

                    output = ''
                    if stdout_task in done:
                        output += stdout_task.result().decode(errors='ignore')
                    if stderr_task in done:
                        output += stderr_task.result().decode(errors='ignore')

                    if not output:
                        if self.current_code and self.input_count > self.input_processed:
                            await asyncio.sleep(0.1)
                            continue
                        logger.info("Execution stream closed")
                        break

                    output = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', output)
                    if output:
                        formatted_output = output
                        if self.input_count > self.input_processed:
                            formatted_output = output.rstrip() + ' >>> '
                        if 'Traceback' in output:
                            if 'SyntaxError' in output:
                                formatted_output = 'Error: Check for missing colons, parentheses, or indentation.'
                            elif 'NameError' in output:
                                formatted_output = 'Error: You used a variable that wasn’t defined.'
                            self.input_count = 0
                            self.input_processed = 0
                        logger.info(f"Sending output: {formatted_output}")
                        await self.send(text_data=json.dumps({'output': formatted_output}))
                except asyncio.TimeoutError:
                    logger.warning("Timeout in stream_output, continuing")
                    continue
                except Exception as e:
                    logger.error(f"Error streaming output: {str(e)}")
                    await self.send(text_data=json.dumps({'error': f'Streaming error: {str(e)}'}))
                    break
        finally:
            if self.input_queue.empty() and self.process and self.process.returncode is not None:
                await self.cleanup_process()

    async def process_input(self):
        try:
            while self.is_running and self.process.returncode is None:
                input_data = await self.input_queue.get()
                if not input_data.endswith('\n'):
                    input_data += '\n'
                try:
                    self.process.stdin.write(input_data.encode())
                    await self.process.stdin.drain()
                    logger.info(f"Sent input: {input_data.strip()}")
                except Exception as e:
                    logger.error(f"Error sending input: {str(e)}")
                    await self.send(text_data=json.dumps({'error': f'Input error: {str(e)}'}))
                finally:
                    self.input_queue.task_done()
        except Exception as e:
            logger.error(f"Error in process_input: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Input processing error: {str(e)}'}))
        finally:
            if self.input_queue.empty() and self.process and self.process.returncode is not None:
                await self.cleanup_process()

    async def watchdog(self, timeout=60):
        await asyncio.sleep(timeout)
        if self.is_running and self.process and self.process.returncode is None:
            logger.warning("Process timeout, terminating")
            await self.send(text_data=json.dumps({'error': 'Process timed out'}))
            await self.cleanup_process()

    async def cleanup_process(self):
        if self.process and self.process.returncode is None:
            try:
                # Terminate process group
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                await self.process.wait()
            except Exception as e:
                logger.error(f"Error cleaning up process: {str(e)}")
        self.process = None
        if self.temp_file_path and os.path.exists(self.temp_file_path):
            try:
                os.remove(self.temp_file_path)
            except Exception as e:
                logger.error(f"Error deleting temp file: {str(e)}")
        self.temp_file_path = None
        self.is_running = False
        self.current_code = None
        self.input_count = 0
        self.input_processed = 0
        while not self.input_queue.empty():
            self.input_queue.get_nowait()
            self.input_queue.task_done()
        logger.info("Process cleaned up")
