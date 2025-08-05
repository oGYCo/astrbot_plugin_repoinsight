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
        
        # 获取配置参数
        self.api_base_url = self.plugin_config.get("api_base_url", "http://api:8000") if self.plugin_config else "http://api:8000"
        self.timeout = self.plugin_config.get("timeout", 30) if self.plugin_config else 30
        self.query_timeout = self.plugin_config.get("query_timeout", 600) if self.plugin_config else 600  # 查询超时设为10分钟
        self.poll_interval = self.plugin_config.get("poll_interval", 5) if self.plugin_config else 5
        
        # Embedding配置 - 使用平级配置格式
        self.embedding_config = {
            'provider': self.plugin_config.get("embedding_provider", "qwen") if self.plugin_config else "qwen",
            'model_name': self.plugin_config.get("embedding_model", "text-embedding-v4") if self.plugin_config else "text-embedding-v4",
            'api_key': self.plugin_config.get("embedding_api_key", "") if self.plugin_config else "",
            'api_base': self.plugin_config.get("embedding_base_url", "") if self.plugin_config else "",
            'extra_params': {}
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
            yield event.plain_result("请发送您要分析的 GitHub 仓库 URL\n💡 分析完成后，您可以随时发送新的仓库URL或 '/repo_qa' 命令来切换仓库")
            
            @session_waiter(record_history_chains=False)
            async def repo_qa_waiter(controller: SessionController, event: AstrMessageEvent):
                user_input = event.message_str.strip()
                
                # 检查是否要退出
                if user_input.lower() in ['退出', 'exit', 'quit', '取消']:
                    await event.send(event.plain_result("👋 已退出 RepoInsight 会话"))
                    controller.stop()
                    return
                
                # 验证GitHub URL
                if not self._is_valid_github_url(user_input):
                    await event.send(event.plain_result(
                        "❌ 请输入有效的 GitHub 仓库 URL\n\n"
                        "示例: https://github.com/user/repo\n\n"
                        "或发送 '退出' 结束会话"
                    ))
                    controller.keep(reset_timeout=True)
                    return
                
                repo_url = user_input
                
                # 检查仓库是否已经分析过 - 先尝试直接查询
                await event.send(event.plain_result(f"仓库正在分析，⏳请稍候"))
                
                try:
                    # 启动仓库分析（后端会自动处理重复请求）
                    analysis_session_id = await self._start_repository_analysis(repo_url)
                    if not analysis_session_id:
                        await event.send(event.plain_result("❌ 启动仓库分析失败，请稍后重试"))
                        controller.stop()
                        return
                    
                    # 保存任务状态
                    await self.state_manager.add_task(analysis_session_id, repo_url, event.unified_msg_origin)
                    
                    # 轮询分析状态
                    analysis_result = await self._poll_analysis_status(analysis_session_id, event)
                    if not analysis_result:
                        await self.state_manager.remove_task(analysis_session_id)
                        controller.stop()
                        return
                    
                    # 分析完成，进入问答模式
                    await event.send(event.plain_result(
                        f"✅ 仓库分析完成！现在您可以开始提问了！\n"
                        f"💡 **提示:**\n"
                        f"• 发送问题进行仓库问答\n"
                        f"• 发送 '/repo_qa' 切换到新仓库\n"
                        f"• 发送 '退出' 结束会话"
                    ))
                    
                    # 进入问答循环，使用仓库URL作为session_id
                    await self._enter_qa_loop(controller, event, repo_url)
                    
                except Exception as e:
                    logger.error(f"仓库处理过程出错: {e}")
                    await event.send(event.plain_result(f"❌ 处理过程出错: {str(e)}"))
                    controller.stop()
            
            try:
                await repo_qa_waiter(event)
            except Exception as e:
                logger.error(f"会话处理出错: {e}")
                yield event.plain_result(f"❌ 会话处理出错: {str(e)}")
            finally:
                event.stop_event()
                
        except Exception as e:
            logger.error(f"启动仓库问答会话失败: {e}")
            yield event.plain_result(f"❌ 启动会话失败: {str(e)}")
    
    async def _enter_qa_loop(self, controller: SessionController, event: AstrMessageEvent, session_id: str):
        """进入问答循环"""
        # 用于跟踪正在处理的问题，防止并发处理同一问题
        processing_questions = set()
        
        # 创建嵌套的session_waiter来处理问答循环 - 无超时限制
        @session_waiter(record_history_chains=False)
        async def qa_loop_waiter(qa_controller: SessionController, qa_event: AstrMessageEvent):
            user_question = qa_event.message_str.strip()
            
            # 检查是否为空消息
            if not user_question:
                await qa_event.send(qa_event.plain_result("请输入您的问题，或发送 '退出' 结束会话，或发送 '/repo_qa' 切换仓库"))
                qa_controller.keep(reset_timeout=True)
                return
            
            # 检查是否为退出命令
            if user_question.lower() in ['退出', 'exit', 'quit', '取消']:
                await qa_event.send(qa_event.plain_result("👋 感谢使用 RepoInsight！"))
                # 如果session_id是URL格式，则不需要从任务管理器中移除
                if session_id.startswith('http'):
                    logger.info(f"结束仓库问答会话: {session_id}")
                else:
                    await self.state_manager.remove_task(session_id)
                qa_controller.stop()
                controller.stop()  # 同时停止外层控制器
                return
            
            # 检查是否为切换仓库命令
            if user_question.lower().startswith('/repo_qa') or user_question.lower().startswith('repo_qa'):
                await qa_event.send(qa_event.plain_result("🔄 正在切换到新的仓库分析...\n\n请发送您要分析的 GitHub 仓库 URL："))
                qa_controller.stop()  # 停止当前问答循环
                controller.stop()     # 停止外层控制器，这会触发新的 repo_qa_session
                return
            
            # 检查是否直接输入了新的GitHub URL（快速切换仓库）
            if self._is_valid_github_url(user_question) and user_question != session_id:
                await qa_event.send(qa_event.plain_result(f"🔄 检测到新仓库URL，正在切换分析...\n\n🔗 新仓库: {user_question}"))
                # 直接开始新仓库的分析流程
                try:
                    analysis_session_id = await self._start_repository_analysis(user_question)
                    if not analysis_session_id:
                        await qa_event.send(qa_event.plain_result("❌ 启动新仓库分析失败，继续使用当前仓库"))
                        qa_controller.keep(reset_timeout=True)
                        return
                    
                    await self.state_manager.add_task(analysis_session_id, user_question, qa_event.unified_msg_origin)
                    
                    analysis_result = await self._poll_analysis_status(analysis_session_id, qa_event)
                    if not analysis_result:
                        await self.state_manager.remove_task(analysis_session_id)
                        await qa_event.send(qa_event.plain_result("❌ 新仓库分析失败，继续使用当前仓库"))
                        qa_controller.keep(reset_timeout=True)
                        return
                    
                    await qa_event.send(qa_event.plain_result(
                        f"✅ 新仓库分析完成！已切换到新仓库\n"
                    ))
                    
                    # 更新session_id为新仓库URL，重启问答循环
                    qa_controller.stop()
                    # 启动新的问答循环
                    await self._enter_qa_loop(controller, qa_event, user_question)
                    return
                except Exception as e:
                    logger.error(f"切换仓库时出错: {e}")
                    await qa_event.send(qa_event.plain_result(f"❌ 切换仓库失败: {str(e)}\n\n继续使用当前仓库"))
                    qa_controller.keep(reset_timeout=True)
                    return
            
            # 检查是否正在处理相同问题（防止并发处理）
            question_hash = hash(user_question)
            
            if question_hash in processing_questions:
                logger.debug(f"问题正在处理中: {user_question}")
                await qa_event.send(qa_event.plain_result("此问题正在处理中，请稍候..."))
                qa_controller.keep(reset_timeout=True)
                return
            
            # 标记问题为正在处理
            processing_questions.add(question_hash)
            logger.info(f"开始处理问题: {user_question[:50]}... - 仓库: {session_id}")
                 
            try:
                # 提交查询请求，使用session_id（可能是URL或分析会话ID）
                query_session_id = await self._submit_query(session_id, user_question)
                if not query_session_id:
                    await qa_event.send(qa_event.plain_result("❌ 提交问题失败，请重试"))
                    qa_controller.keep(reset_timeout=True)
                    return
                
                # 轮询查询结果
                answer = await self._poll_query_result(query_session_id, qa_event)
                if answer:
                    # 智能分段发送长回答
                    await self._send_long_message(qa_event, f"💡 **回答:**\n\n{answer}")
                else:
                    await qa_event.send(qa_event.plain_result("❌ 获取答案失败，请重试"))
                
                # 继续等待下一个问题
                qa_controller.keep(reset_timeout=True)
                return  # 重要：必须return，否则函数会结束导致session结束
                
            except Exception as e:
                logger.error(f"处理问题时出错: {e}")
                await qa_event.send(qa_event.plain_result(f"❌ 处理问题时出错: {str(e)}"))
                qa_controller.keep(reset_timeout=True)
                return  # 重要：必须return，否则函数会结束导致session结束
            finally:
                # 无论成功还是失败，都要移除正在处理标记
                processing_questions.discard(question_hash)
        
        # 启动问答循环
        try:
            await qa_loop_waiter(event)
        except Exception as e:
            logger.error(f"问答循环出错: {e}")
            await event.send(event.plain_result(f"❌ 问答循环出错: {str(e)}"))
            # 如果session_id不是URL格式，才从任务管理器中移除
            if not session_id.startswith('http'):
                await self.state_manager.remove_task(session_id)
    
    def _is_valid_github_url(self, url: str) -> bool:
        """验证GitHub URL格式"""
        github_pattern = r'^https://github\.com/[\w\.-]+/[\w\.-]+/?$'
        return bool(re.match(github_pattern, url))
    
    async def _start_repository_analysis(self, repo_url: str) -> Optional[str]:
        """启动仓库分析"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                payload = {
                    "repo_url": repo_url,
                    "embedding_config": self.embedding_config
                }
                
                async with session.post(
                    f"{self.api_base_url}/api/v1/repos/analyze",
                    json=payload,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        return result.get('session_id')
                    else:
                        error_text = await response.text()
                        logger.error(f"启动分析失败: {response.status} - {error_text}")
                        return None
        except Exception as e:
            logger.error(f"启动仓库分析请求失败: {e}")
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
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.query_timeout)) as session:
                payload = {
                    "session_id": session_id,
                    "question": question,
                    "generation_mode": "service",
                    "llm_config": self.llm_config
                }
                
                async with session.post(
                    f"{self.api_base_url}/api/v1/repos/query",
                    json=payload,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        return result.get('session_id')  # 这是查询的session_id
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
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.query_timeout)) as session:
                while True:
                    # 先检查状态
                    async with session.get(
                        f"{self.api_base_url}/api/v1/repos/query/status/{query_session_id}"
                    ) as response:
                        if response.status == 200:
                            status_result = await response.json()
                            status = status_result.get('status')
                            
                            if status == 'success':
                                # 获取结果
                                async with session.get(
                                    f"{self.api_base_url}/api/v1/repos/query/result/{query_session_id}"
                                ) as result_response:
                                    if result_response.status == 200:
                                        result = await result_response.json()
                                        
                                        # 如果是plugin模式，需要自己生成答案
                                        if result.get('generation_mode') == 'plugin':
                                            return await self._generate_answer_from_context(
                                                result.get('retrieved_context', []),
                                                result.get('question', '')
                                            )
                                        else:
                                            return result.get('answer', '未获取到答案')
                                    else:
                                        logger.error(f"获取查询结果失败: {result_response.status}")
                                        return None
                            elif status == 'failed':
                                error_msg = status_result.get('message', '查询失败')
                                logger.error(f"查询失败: {error_msg}")
                                return None
                            elif status in ['queued', 'processing', 'started', 'pending']:
                                await asyncio.sleep(2)  # 查询轮询间隔更短
                            else:
                                logger.error(f"未知查询状态: {status}")
                                return None
                        else:
                            logger.error(f"查询状态检查失败: {response.status}")
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
    
    def _ensure_data_dir(self):
        """确保data目录存在"""
        os.makedirs("data", exist_ok=True)
    
    async def _init_db(self):
        """初始化数据库"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS analysis_tasks (
                        session_id TEXT PRIMARY KEY,
                        repo_url TEXT NOT NULL,
                        user_origin TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        status TEXT DEFAULT 'pending'
                    )
                """)
                await db.commit()
        except ImportError:
            logger.warning("aiosqlite未安装，状态持久化功能将不可用")
        except Exception as e:
            logger.error(f"初始化数据库失败: {e}")
    
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
