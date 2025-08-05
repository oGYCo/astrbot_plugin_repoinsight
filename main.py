from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
import astrbot.api.message_components as Comp
from astrbot.core.utils.session_waiter import (
    session_waiter,
    SessionController,
)
import asyncio
import aiohttp
import json
import re
import time
from typing import Optional, Dict, Any
from datetime import datetime
import os
import aiosqlite


@register("RepoInsight", "oGYCo", "GitHub仓库智能问答插件，支持仓库分析和智能问答", "1.0.0")
class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        
        # 初始化配置
        self.plugin_config = config or {}
        self.astrbot_config = config
        
        # 输出调试信息
        logger.info("=== RepoInsight插件开始初始化 ===")
        logger.info(f"配置信息: {self.plugin_config}")
        
        # 获取配置参数
        self.api_base_url = self.plugin_config.get("api_base_url", "http://api:8000") if self.plugin_config else "http://api:8000"
        self.timeout = self.plugin_config.get("timeout", 30) if self.plugin_config else 30
        self.query_timeout = self.plugin_config.get("query_timeout", 600) if self.plugin_config else 600  # 查询超时设为10分钟
        self.poll_interval = self.plugin_config.get("poll_interval", 5) if self.plugin_config else 5
        
        # Embedding配置 - 使用平级配置格式
        self.embedding_config = {
            'provider': self.plugin_config.get("embedding_provider", "qwen") if self.plugin_config else "qwen",
            'model_name': self.plugin_config.get("embedding_model", "text-embedding-v4") if self.plugin_config else "text-embedding-v4",
            'api_key': self.plugin_config.get("embedding_api_key", "") if self.plugin_config else ""
        }
        
        # LLM配置 - 使用平级配置格式
        self.llm_config = {
            'provider': self.plugin_config.get("llm_provider", "qwen") if self.plugin_config else "qwen",
            'model_name': self.plugin_config.get("llm_model", "qwen-plus") if self.plugin_config else "qwen-plus",
            'api_key': self.plugin_config.get("llm_api_key", "") if self.plugin_config else "",
            'temperature': self.plugin_config.get("llm_temperature", 0.7) if self.plugin_config else 0.7,
            'max_tokens': self.plugin_config.get("llm_max_tokens", 9000) if self.plugin_config else 9000
        }
        
        # 初始化状态管理器
        self.state_manager = StateManager()
        
        # 启动时恢复未完成的任务
        asyncio.create_task(self._restore_pending_tasks())
        
        logger.info("RepoInsight插件已初始化")
    
    async def _restore_pending_tasks(self):
        """恢复插件重启前未完成的任务"""
        try:
            pending_tasks = await self.state_manager.get_all_pending_tasks()
            for task in pending_tasks:
                logger.info(f"恢复任务: {task['session_id']} - {task['repo_url']}")
                # 这里可以添加恢复逻辑，比如重新检查任务状态
        except Exception as e:
            logger.error(f"恢复任务失败: {e}")
    
    @filter.command("repo_qa")
    async def repo_qa_session(self, event: AstrMessageEvent):
        """启动仓库问答会话"""
        try:
            logger.info("=== 收到 /repo_qa 命令，启动仓库问答会话 ===")
            logger.info(f"用户: {event.unified_msg_origin}")
            logger.info(f"消息内容: {event.message_str}")
            
            # 发送初始消息
            await event.send(event.plain_result("请发送您要分析的 GitHub 仓库 URL\n💡 分析完成后，您可以随时发送新的仓库URL或 '/repo_qa' 命令来切换仓库"))
            
            # 使用正确的session_waiter模式
            @session_waiter(timeout=7200)
            async def session_handler(controller: SessionController, event: AstrMessageEvent):
                """处理会话的函数 - 使用状态管理的事件驱动模式"""
                logger.info(f"进入session_handler，当前状态: {self.state_manager.user_states}")
                
                # 获取或初始化当前用户的状态
                user_id = event.unified_msg_origin
                user_state = await self.state_manager.get_user_state(user_id)
                
                # 重要：禁止AstrBot默认的LLM调用，避免冲突
                event.should_call_llm(False)
                
                user_input = event.message_str.strip()
                
                # 检查是否为空消息
                if not user_input:
                    if user_state.get('current_repo_url'):
                        await event.send(event.plain_result("请输入您的问题，或发送 '退出' 结束会话，或发送 '/repo_qa' 切换仓库"))
                    else:
                        await event.send(event.plain_result("请发送您要分析的 GitHub 仓库 URL"))
                    return
                
                # 检查是否为退出命令
                if user_input.lower() in ['退出', 'exit', 'quit', '取消']:
                    await event.send(event.plain_result("👋 感谢使用 RepoInsight！"))
                    if user_state.get('analysis_session_id'):
                        await self.state_manager.remove_task(user_state['analysis_session_id'])
                    await self.state_manager.clear_user_state(user_id)
                    controller.stop()
                    return
                
                # 检查是否为切换仓库命令
                if user_input.lower().startswith('/repo_qa') or user_input.lower().startswith('repo_qa'):
                    await event.send(event.plain_result("🔄 请发送您要分析的新 GitHub 仓库 URL："))
                    # 重置状态
                    await self.state_manager.clear_user_state(user_id)
                    return
                
                # 如果还没有分析仓库，或者用户输入了新的GitHub URL
                if not user_state.get('current_repo_url') or self._is_valid_github_url(user_input):
                    # 验证GitHub URL
                    if not self._is_valid_github_url(user_input):
                        await event.send(event.plain_result(
                            "❌ 请输入有效的 GitHub 仓库 URL\n\n"
                            "示例: https://github.com/user/repo\n\n"
                            "或发送 '退出' 结束会话"
                        ))
                        return
                    
                    repo_url = user_input
                    logger.info(f"开始处理仓库URL: {repo_url}")
                    
                    # 如果是切换到新仓库
                    current_repo_url = user_state.get('current_repo_url')
                    if current_repo_url and repo_url != current_repo_url:
                        await event.send(event.plain_result(f"🔄 检测到新仓库URL，正在切换分析...\n\n🔗 新仓库: {repo_url}"))
                    else:
                        await event.send(event.plain_result(f"🔍 开始分析仓库，⏳请稍候..."))
                    
                    try:
                        # 启动仓库分析
                        logger.info(f"启动仓库分析: {repo_url}")
                        new_analysis_session_id = await self._start_repository_analysis(repo_url)
                        logger.info(f"分析会话ID: {new_analysis_session_id}")
                        
                        if not new_analysis_session_id:
                            logger.error("启动仓库分析失败")
                            await event.send(event.plain_result("❌ 启动仓库分析失败，请稍后重试或尝试其他仓库"))
                            return
                        
                        # 保存任务状态
                        await self.state_manager.add_task(new_analysis_session_id, repo_url, user_id)
                        
                        # 轮询分析状态
                        analysis_result = await self._poll_analysis_status(new_analysis_session_id, event)
                        if not analysis_result:
                            await self.state_manager.remove_task(new_analysis_session_id)
                            await event.send(event.plain_result("❌ 仓库分析失败，请稍后重试或尝试其他仓库"))
                            return
                        
                        # 分析成功，更新用户状态
                        await self.state_manager.set_user_state(user_id, {
                            'current_repo_url': repo_url,
                            'analysis_session_id': new_analysis_session_id,
                            'processing_questions': set()
                        })
                        
                        await event.send(event.plain_result(
                            f"✅ 仓库分析完成！现在您可以开始提问了！\n"
                            f"💡 **提示:**\n"
                            f"• 发送问题进行仓库问答\n"
                            f"• 发送新的仓库URL可以快速切换\n"
                            f"• 发送 '/repo_qa' 切换到新仓库\n"
                            f"• 发送 '退出' 结束会话"
                        ))
                        return
                        
                    except Exception as e:
                        logger.error(f"仓库处理过程出错: {e}")
                        await event.send(event.plain_result(f"❌ 处理过程出错: {str(e)}"))
                        return
                
                # 如果已经有分析好的仓库，处理用户问题
                elif user_state.get('current_repo_url') and user_state.get('analysis_session_id'):
                    user_question = user_input
                    current_repo_url = user_state['current_repo_url']
                    analysis_session_id = user_state['analysis_session_id']
                    processing_questions = user_state.get('processing_questions', set())
                    
                    # 检查是否正在处理相同问题（防止并发处理）
                    question_hash = hash(user_question)
                    
                    if question_hash in processing_questions:
                        logger.info(f"问题正在处理中: {user_question}")
                        await event.send(event.plain_result("此问题正在处理中，请稍候..."))
                        return
                    
                    # 标记问题为正在处理
                    processing_questions.add(question_hash)
                    await self.state_manager.set_user_state(user_id, {
                        **user_state,
                        'processing_questions': processing_questions
                    })
                    
                    logger.info(f"开始处理问题: {user_question[:50]}... - 仓库: {current_repo_url}")
                         
                    try:
                        # 提交查询请求，使用仓库URL作为session_id
                        query_session_id = await self._submit_query(analysis_session_id, user_question)
                        if not query_session_id:
                            await event.send(event.plain_result("❌ 提交问题失败，请重试"))
                            return
                        
                        # 轮询查询结果
                        answer = await self._poll_query_result(query_session_id, event)
                        if answer:
                            # 智能分段发送长回答
                            await self._send_long_message(event, f"💡 **回答:**\n\n{answer}")
                        else:
                            await event.send(event.plain_result("❌ 获取答案失败，请重试"))
                        
                        return
                        
                    except Exception as e:
                        logger.error(f"处理问题时出错: {e}")
                        await event.send(event.plain_result(f"❌ 处理问题时出错: {str(e)}"))
                        return
                    finally:
                        # 无论成功还是失败，都要移除正在处理标记
                        processing_questions.discard(question_hash)
                        await self.state_manager.set_user_state(user_id, {
                            **user_state,
                            'processing_questions': processing_questions
                        })
                
                else:
                    # 应该不会到达这里，但保险起见
                    await event.send(event.plain_result("请发送您要分析的 GitHub 仓库 URL"))
                    return
            
            # 启动会话处理器
            try:
                await session_handler(event)
            except TimeoutError:
                await event.send(event.plain_result("⏰ 会话超时，请重新发送 /repo_qa 命令开始新的会话"))
            except Exception as e:
                logger.error(f"会话处理器异常: {e}")
                await event.send(event.plain_result(f"❌ 会话异常: {str(e)}"))
            finally:
                # 清理会话状态
                event.stop_event()
            
        except Exception as e:
            logger.error(f"启动仓库问答会话失败: {e}")
            await event.send(event.plain_result(f"❌ 启动会话失败: {str(e)}"))
    
    def _is_valid_github_url(self, url: str) -> bool:
        """验证GitHub URL格式"""
        github_pattern = r'^https://github\.com/[\w\.-]+/[\w\.-]+/?$'
        return bool(re.match(github_pattern, url))
    
    async def _start_repository_analysis(self, repo_url: str) -> Optional[str]:
        """启动仓库分析"""
        try:
            logger.info(f"=== 开始启动仓库分析 ===")
            logger.info(f"仓库URL: {repo_url}")
            logger.info(f"API地址: {self.api_base_url}")
            logger.info(f"超时设置: {self.timeout}秒")
            
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                payload = {
                    "repo_url": repo_url,
                    "embedding_config": self.embedding_config
                }
                
                logger.info(f"请求载荷: {payload}")
                
                async with session.post(
                    f"{self.api_base_url}/api/v1/repos/analyze",
                    json=payload,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    logger.info(f"HTTP响应状态: {response.status}")
                    
                    if response.status == 200:
                        result = await response.json()
                        session_id = result.get('session_id')
                        logger.info(f"分析启动成功，会话ID: {session_id}")
                        logger.info(f"完整响应: {result}")
                        return session_id
                    else:
                        error_text = await response.text()
                        logger.error(f"启动分析失败: {response.status} - {error_text}")
                        return None
        except Exception as e:
            logger.error(f"启动仓库分析请求失败: {e}")
            logger.error(f"异常详情: {str(e)}")
            return None
    
    async def _poll_analysis_status(self, session_id: str, event: AstrMessageEvent) -> Optional[Dict[str, Any]]:
        """轮询分析状态"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.query_timeout)) as session:
                while True:
                    async with session.get(
                        f"{self.api_base_url}/api/v1/repos/status/{session_id}"
                    ) as response:
                        if response.status == 200:
                            result = await response.json()
                            status = result.get('status')
                            
                            if status == 'success':
                                return result
                            elif status == 'failed':
                                error_msg = result.get('error_message', '未知错误')
                                await event.send(event.plain_result(f"❌ 分析失败: {error_msg}"))
                                return None
                            elif status in ['queued', 'processing']:
                                # 静默等待，不发送进度消息
                                pass
                            
                            await asyncio.sleep(self.poll_interval)
                        else:
                            logger.error(f"查询分析状态失败: {response.status}")
                            return None
        except Exception as e:
            logger.error(f"轮询分析状态失败: {e}")
            return None
    
    async def _submit_query(self, session_id: str, question: str) -> Optional[str]:
        """提交查询请求"""
        try:
            logger.info(f"提交查询请求: session_id={session_id}, question={question[:100]}...")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.query_timeout)) as session:
                payload = {
                    "session_id": session_id,
                    "question": question,
                    "generation_mode": "service",
                    "llm_config": self.llm_config
                }
                
                logger.info(f"请求载荷: {payload}")
                
                async with session.post(
                    f"{self.api_base_url}/api/v1/repos/query",
                    json=payload,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        query_session_id = result.get('session_id')
                        logger.info(f"查询请求提交成功: query_session_id={query_session_id}")
                        return query_session_id  # 这是查询的session_id
                    else:
                        error_text = await response.text()
                        logger.error(f"提交查询失败: {response.status} - {error_text}")
                        return None
        except Exception as e:
            logger.error(f"提交查询请求失败: {e}")
            return None
    
    async def _poll_query_result(self, query_session_id: str, event: AstrMessageEvent) -> Optional[str]:
        """轮询查询结果"""
        try:
            logger.info(f"开始轮询查询结果: {query_session_id}")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.query_timeout)) as session:
                max_polls = 180  # 最多轮询3分钟 (180 * 2秒)
                poll_count = 0
                
                while poll_count < max_polls:
                    poll_count += 1
                    # 先检查状态
                    logger.info(f"轮询第 {poll_count} 次，查询状态: {query_session_id}")
                    
                    async with session.get(
                        f"{self.api_base_url}/api/v1/repos/query/status/{query_session_id}"
                    ) as response:
                        if response.status == 200:
                            status_result = await response.json()
                            status = status_result.get('status')
                            logger.info(f"查询状态: {status}, session_id: {query_session_id}")
                            
                            if status == 'success':
                                # 获取结果
                                logger.info(f"查询成功，获取结果: {query_session_id}")
                                async with session.get(
                                    f"{self.api_base_url}/api/v1/repos/query/result/{query_session_id}"
                                ) as result_response:
                                    if result_response.status == 200:
                                        result = await result_response.json()
                                        logger.info(f"获取结果成功: {len(str(result))} 字符")
                                        
                                        # 如果是plugin模式，需要自己生成答案
                                        if result.get('generation_mode') == 'plugin':
                                            answer = await self._generate_answer_from_context(
                                                result.get('retrieved_context', []),
                                                result.get('question', '')
                                            )
                                            logger.info(f"生成答案完成: {len(answer)} 字符")
                                            return answer
                                        else:
                                            answer = result.get('answer', '未获取到答案')
                                            logger.info(f"直接返回答案: {len(answer)} 字符")
                                            return answer
                                    else:
                                        logger.error(f"获取查询结果失败: {result_response.status}")
                                        error_text = await result_response.text()
                                        logger.error(f"错误详情: {error_text}")
                                        return None
                            elif status == 'failed':
                                error_msg = status_result.get('message', '查询失败')
                                logger.error(f"查询失败: {error_msg}")
                                return None
                            elif status in ['queued', 'processing', 'started', 'pending']:
                                logger.info(f"查询进行中: {status}")
                                await asyncio.sleep(2)  # 查询轮询间隔更短
                                continue
                            else:
                                logger.error(f"未知查询状态: {status}")
                                return None
                        else:
                            logger.error(f"查询状态检查失败: {response.status}")
                            error_text = await response.text()
                            logger.error(f"错误详情: {error_text}")
                            return None
                
                logger.error(f"查询超时: 已轮询 {max_polls} 次，session_id: {query_session_id}")
                return None
                
        except Exception as e:
            logger.error(f"轮询查询结果失败: {e}")
            return None
    
    async def _send_long_message(self, event: AstrMessageEvent, message: str, max_length: int = 1800):
        """智能分段发送长消息，确保完整性和内容不丢失"""
        if len(message) <= max_length:
            await event.send(event.plain_result(message))
            return
        
        # 安全分段算法 - 确保不丢失任何内容
        parts = []
        remaining_text = message
        
        while len(remaining_text) > max_length:
            # 在最大长度范围内寻找最佳分割点
            search_end = max_length
            best_split_pos = None
            
            # 优先级1: 段落边界（双换行符）
            double_newline_pos = remaining_text.rfind('\n\n', 0, search_end)
            if double_newline_pos > max_length // 3:  # 确保分割点不会太靠前
                best_split_pos = double_newline_pos + 2
            
            # 优先级2: 单换行符
            if best_split_pos is None:
                single_newline_pos = remaining_text.rfind('\n', max_length // 2, search_end)
                if single_newline_pos > 0:
                    best_split_pos = single_newline_pos + 1
            
            # 优先级3: 句号等句子结束符
            if best_split_pos is None:
                for delimiter in ['。', '！', '？', '.', '!', '?']:
                    delimiter_pos = remaining_text.rfind(delimiter, max_length // 2, search_end)
                    if delimiter_pos > 0:
                        best_split_pos = delimiter_pos + 1
                        break
            
            # 优先级4: 逗号等标点符号
            if best_split_pos is None:
                for delimiter in ['，', ',', '；', ';', '：', ':']:
                    delimiter_pos = remaining_text.rfind(delimiter, max_length // 2, search_end)
                    if delimiter_pos > 0:
                        best_split_pos = delimiter_pos + 1
                        break
            
            # 优先级5: 空格
            if best_split_pos is None:
                space_pos = remaining_text.rfind(' ', max_length // 2, search_end)
                if space_pos > 0:
                    best_split_pos = space_pos + 1
            
            # 如果找不到合适的分割点，就在最大长度处强制分割
            if best_split_pos is None:
                best_split_pos = max_length
            
            # 提取当前部分
            current_part = remaining_text[:best_split_pos].strip()
            if current_part:  # 只添加非空内容
                parts.append(current_part)
            
            # 更新剩余文本
            remaining_text = remaining_text[best_split_pos:].strip()
        
        # 添加剩余的所有内容
        if remaining_text.strip():
            parts.append(remaining_text.strip())
        
        # 发送所有部分
        for i, part in enumerate(parts):
            if len(parts) > 1:
                # 添加分页标记
                part_header = f"📄 (第{i+1}部分，共{len(parts)}部分)\n\n"
                final_part = part_header + part
            else:
                final_part = part
            
            await event.send(event.plain_result(final_part))
            
            # 在多段消息之间稍作延迟，避免消息顺序混乱
            if i < len(parts) - 1:
                await asyncio.sleep(0.3)
        
        # 验证内容完整性（仅在调试模式下）
        total_original_length = len(message.replace(' ', '').replace('\n', ''))
        total_parts_length = len(''.join(parts).replace(' ', '').replace('\n', ''))
        if total_original_length != total_parts_length:
            logger.warning(f"分段可能丢失内容: 原始长度={total_original_length}, 分段后长度={total_parts_length}")
    
    async def _generate_answer_from_context(self, context_list: list, question: str) -> str:
        """基于检索到的上下文生成答案"""
        try:
            if not context_list:
                return "抱歉，没有找到相关的代码信息来回答您的问题。"
            
            # 构建上下文字符串
            context_str = "\n\n".join([
                f"文件: {ctx.get('file_path', 'Unknown')}\n内容: {ctx.get('content', '')}"
                for ctx in context_list[:5]  # 限制上下文数量
            ])
            
            # 构建提示词
            prompt = f"""基于以下代码上下文回答用户问题：

上下文：
{context_str}

用户问题：{question}

请基于提供的代码上下文给出准确、详细的回答。如果上下文中没有足够信息回答问题，请说明这一点。"""
            
            # 使用AstrBot的LLM功能生成答案
            provider = self.context.get_using_provider()
            if provider:
                response = await provider.text_chat(
                    prompt=prompt,
                    session_id=None,
                    contexts=[],
                    image_urls=[],
                    system_prompt="你是一个专业的代码分析助手，能够基于提供的代码上下文回答用户的问题。"
                )
                return response.completion_text if response else "生成答案失败"
            else:
                # 如果没有配置LLM，返回简单的上下文摘要
                return f"找到了 {len(context_list)} 个相关代码片段：\n\n" + "\n\n".join([
                    f"📁 {ctx.get('file_path', 'Unknown')}\n{ctx.get('content', '')[:200]}..."
                    for ctx in context_list[:3]
                ])
        except Exception as e:
            logger.error(f"生成答案失败: {e}")
            return f"生成答案时出错: {str(e)}"
    
    @filter.command("repo_test")
    async def test_plugin(self, event: AstrMessageEvent):
        """测试插件是否正常工作"""
        try:
            logger.info("=== 测试命令被调用 ===")
            yield event.plain_result(f"✅ RepoInsight插件工作正常！\n\n配置信息:\n• API地址: {self.api_base_url}\n• 超时设置: {self.timeout}秒")
        except Exception as e:
            logger.error(f"测试命令失败: {e}")
            yield event.plain_result(f"❌ 测试失败: {str(e)}")
    
    @filter.command("repo_status")
    async def check_repo_status(self, event: AstrMessageEvent):
        """查看当前用户的仓库分析状态"""
        try:
            tasks = await self.state_manager.get_user_tasks(event.unified_msg_origin)
            if not tasks:
                yield event.plain_result("📋 您当前没有进行中的仓库分析任务")
                return
            
            status_text = "📊 **您的仓库分析状态:**\n\n"
            for task in tasks:
                status_text += f"• 仓库: {task['repo_url']}\n"
                status_text += f"  会话ID: {task['session_id']}\n"
                status_text += f"  创建时间: {task['created_at']}\n\n"
            
            yield event.plain_result(status_text)
        except Exception as e:
            logger.error(f"查看状态失败: {e}")
            yield event.plain_result(f"❌ 查看状态失败: {str(e)}")
    
    @filter.command("repo_config")
    async def show_config(self, event: AstrMessageEvent):
        """显示当前配置"""
        try:
            config_text = f"""⚙️ **RepoInsight 配置信息:**

**API 配置:**
• 服务地址: {self.api_base_url}
• 分析超时: {self.timeout}秒
• 查询超时: {self.query_timeout}秒
• 轮询间隔: {self.poll_interval}秒

**Embedding 配置:**
• 提供商: {self.embedding_config.get('provider', 'Unknown')}
• 模型: {self.embedding_config.get('model_name', 'Unknown')}

**LLM 配置:**
• 提供商: {self.llm_config.get('provider', 'Unknown')}
• 模型: {self.llm_config.get('model_name', 'Unknown')}
• 温度: {self.llm_config.get('temperature', 0.7)}
• 最大令牌: {self.llm_config.get('max_tokens', 2000)}"""
            
            yield event.plain_result(config_text)
        except Exception as e:
            logger.error(f"显示配置失败: {e}")
            yield event.plain_result(f"❌ 显示配置失败: {str(e)}")
    
    async def terminate(self):
        """插件终止时的清理工作"""
        try:
            await self.state_manager.close()
            logger.info("RepoInsight插件已清理完成")
        except Exception as e:
            logger.error(f"插件清理失败: {e}")


class StateManager:
    """状态持久化管理器"""
    
    def __init__(self):
        self.db_path = os.path.join("data", "repoinsight_tasks.db")
        self._ensure_data_dir()
        self._init_db_task = asyncio.create_task(self._init_db())
        # 内存中的用户状态缓存
        self.user_states = {}
    
    def _ensure_data_dir(self):
        """确保data目录存在"""
        os.makedirs("data", exist_ok=True)
    
    async def _init_db(self):
        """初始化数据库"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # 分析任务表
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS analysis_tasks (
                        session_id TEXT PRIMARY KEY,
                        repo_url TEXT NOT NULL,
                        user_origin TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        status TEXT DEFAULT 'pending'
                    )
                """)
                # 用户状态表
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS user_states (
                        user_id TEXT PRIMARY KEY,
                        current_repo_url TEXT,
                        analysis_session_id TEXT,
                        updated_at TEXT NOT NULL
                    )
                """)
                await db.commit()
        except ImportError:
            logger.warning("aiosqlite未安装，状态持久化功能将不可用")
        except Exception as e:
            logger.error(f"初始化数据库失败: {e}")
    
    async def get_user_state(self, user_id: str) -> Dict[str, Any]:
        """获取用户状态"""
        # 首先检查内存缓存
        if user_id in self.user_states:
            return self.user_states[user_id]
        
        # 从数据库读取
        try:
            await self._init_db_task
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT current_repo_url, analysis_session_id FROM user_states WHERE user_id = ?",
                    (user_id,)
                )
                row = await cursor.fetchone()
                if row:
                    state = {
                        'current_repo_url': row[0],
                        'analysis_session_id': row[1],
                        'processing_questions': set()
                    }
                    self.user_states[user_id] = state
                    return state
        except Exception as e:
            logger.error(f"获取用户状态失败: {e}")
        
        # 返回默认状态
        default_state = {
            'current_repo_url': None,
            'analysis_session_id': None,
            'processing_questions': set()
        }
        self.user_states[user_id] = default_state
        return default_state
    
    async def set_user_state(self, user_id: str, state: Dict[str, Any]):
        """设置用户状态"""
        # 更新内存缓存
        self.user_states[user_id] = state
        
        # 保存到数据库
        try:
            await self._init_db_task
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO user_states 
                    (user_id, current_repo_url, analysis_session_id, updated_at) 
                    VALUES (?, ?, ?, ?)
                """, (
                    user_id,
                    state.get('current_repo_url'),
                    state.get('analysis_session_id'),
                    datetime.now().isoformat()
                ))
                await db.commit()
        except Exception as e:
            logger.error(f"设置用户状态失败: {e}")
    
    async def clear_user_state(self, user_id: str):
        """清除用户状态"""
        # 清除内存缓存
        self.user_states.pop(user_id, None)
        
        # 从数据库删除
        try:
            await self._init_db_task
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))
                await db.commit()
        except Exception as e:
            logger.error(f"清除用户状态失败: {e}")
    
    async def add_task(self, session_id: str, repo_url: str, user_origin: str):
        """添加分析任务"""
        try:
            await self._init_db_task  # 等待数据库初始化完成
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO analysis_tasks (session_id, repo_url, user_origin, created_at) VALUES (?, ?, ?, ?)",
                    (session_id, repo_url, user_origin, datetime.now().isoformat())
                )
                await db.commit()
        except ImportError:
            pass  # aiosqlite未安装
        except Exception as e:
            logger.error(f"添加任务失败: {e}")
    
    async def remove_task(self, session_id: str):
        """移除分析任务"""
        try:
            await self._init_db_task
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM analysis_tasks WHERE session_id = ?", (session_id,))
                await db.commit()
        except ImportError:
            pass
        except Exception as e:
            logger.error(f"移除任务失败: {e}")
    
    async def get_all_pending_tasks(self):
        """获取所有待处理任务"""
        try:
            await self._init_db_task
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("SELECT * FROM analysis_tasks WHERE status = 'pending'")
                rows = await cursor.fetchall()
                return [
                    {
                        'session_id': row[0],
                        'repo_url': row[1],
                        'user_origin': row[2],
                        'created_at': row[3],
                        'status': row[4]
                    }
                    for row in rows
                ]
        except ImportError:
            return []
        except Exception as e:
            logger.error(f"获取待处理任务失败: {e}")
            return []
    
    async def get_user_tasks(self, user_origin: str):
        """获取用户的所有任务"""
        try:
            await self._init_db_task
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT * FROM analysis_tasks WHERE user_origin = ? ORDER BY created_at DESC",
                    (user_origin,)
                )
                rows = await cursor.fetchall()
                return [
                    {
                        'session_id': row[0],
                        'repo_url': row[1],
                        'user_origin': row[2],
                        'created_at': row[3],
                        'status': row[4]
                    }
                    for row in rows
                ]
        except ImportError:
            return []
        except Exception as e:
            logger.error(f"获取用户任务失败: {e}")
            return []
    
    async def close(self):
        """关闭状态管理器"""
        try:
            if hasattr(self, '_init_db_task'):
                await self._init_db_task
        except Exception as e:
            logger.error(f"关闭状态管理器失败: {e}")
