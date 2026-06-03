from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.core.database import init_db
from app.models.schemas import (
    AgentTracePreviewRequest,
    AgentTracePreviewResponse,
    ChatStreamEvent,
    ChannelEventRequest,
    ClaimTaskRequest,
    DashboardMetrics,
    ConversationDetail,
    ConversationListItem,
    DemoScenarioRequest,
    IntentRecognizeRequest,
    KnowledgeDocument,
    KnowledgeImportResult,
    KbVectorIndexBuildResult,
    KbSearchRequest,
    ManualFollowUpReplyRequest,
    ProcessedEventResponse,
    ReplyCheckRequest,
    ReplyGenerateRequest,
    ResolveTaskRequest,
    SystemConfig,
    UpdateSystemConfigRequest,
)
from app.services.demo import DemoService
from app.services.intent import IntentService
from app.services.knowledge_base import KnowledgeBaseService
from app.services.llm_gateway import LlmUnavailableError, require_llm_runtime
from app.services.orchestrator import ConversationOrchestrator
from app.services.quality import QualityService
from app.services.reply import ReplyService
from app.services.store import AgentStore
from app.services.tagging import TaggingService


store = AgentStore()
intent_service = IntentService()
kb_service = KnowledgeBaseService()
reply_service = ReplyService()
quality_service = QualityService()
tagging_service = TaggingService()
orchestrator = ConversationOrchestrator(
    store=store,
    intent_service=intent_service,
    kb_service=kb_service,
    reply_service=reply_service,
    quality_service=quality_service,
    tagging_service=tagging_service,
)
demo_service = DemoService(store=store, orchestrator=orchestrator)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    store.ensure_default_config()
    # 启动时顺手修复历史本地数据，避免表结构升级或演示中断后，
    # 待跟进队列出现缺失、重复或无法关联消息的任务。
    store.backfill_follow_up_task_message_ids()
    store.restore_missing_follow_up_tasks(
        confidence_threshold=store.get_system_config().get("intent_confidence_threshold", 0.7)
    )
    store.cleanup_duplicate_follow_up_tasks()
    yield


app = FastAPI(
    title="小红书商家客服 Agent 中台 MVP",
    version="0.1.0",
    description="支持意图识别、知识检索、自动回复、回复质检和待跟进队列的本地可运行 MVP。",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.exception_handler(LlmUnavailableError)
async def llm_unavailable_handler(_: object, exc: LlmUnavailableError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def console_index():
    with open("static/index.html", "r", encoding="utf-8") as file:
        html = file.read()
    version = "20260602-intent-handoff-fix"
    html = html.replace("/static/styles.css", f"/static/styles.css?v={version}")
    html = html.replace("/static/app.js", f"/static/app.js?v={version}")
    return HTMLResponse(html)


@app.post("/api/channel/xiaohongshu/events", response_model=ProcessedEventResponse)
async def receive_channel_event(request: ChannelEventRequest) -> ProcessedEventResponse:
    return await orchestrator.process_channel_event(request)


@app.post("/api/channel/xiaohongshu/events/stream")
async def receive_channel_event_stream(request: ChannelEventRequest):
    require_llm_runtime(store.get_system_config())

    async def event_generator():
        # 这里的流式输出主要服务前端体验：先立即回显用户消息，
        # 等编排器完成真实处理后，再把最终回复切成小段推送。
        user_event = ChatStreamEvent(
            type="user_message",
            content=request.content,
            conversation_id=request.conversation_id,
            payload={"user_id": request.user_id},
        )
        yield f"data: {user_event.model_dump_json()}\n\n"

        start_event = ChatStreamEvent(type="agent_start", content="Agent 正在思考中...")
        yield f"data: {start_event.model_dump_json()}\n\n"

        result = await orchestrator.process_channel_event(request)
        reply_text = result.reply.draft_reply or ""

        for index in range(0, len(reply_text), 8):
            chunk_event = ChatStreamEvent(
                type="agent_chunk",
                content=reply_text[index : index + 8],
                conversation_id=result.conversation_id,
            )
            yield f"data: {chunk_event.model_dump_json()}\n\n"
            await asyncio.sleep(0.03)

        meta_event = ChatStreamEvent(
            type="meta",
            conversation_id=result.conversation_id,
            payload=result.model_dump(),
        )
        yield f"data: {meta_event.model_dump_json()}\n\n"

        done_event = ChatStreamEvent(
            type="agent_done",
            content="done",
            conversation_id=result.conversation_id,
        )
        yield f"data: {done_event.model_dump_json()}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/demo/seed")
async def seed_demo_data():
    return await demo_service.seed_demo_data()


@app.post("/api/demo/run", response_model=ProcessedEventResponse)
async def run_demo_scenario(request: DemoScenarioRequest) -> ProcessedEventResponse:
    return await demo_service.run_scenario(request)


@app.post("/internal/intent/recognize")
def recognize_intent(request: IntentRecognizeRequest):
    return intent_service.recognize(request)


@app.post("/internal/kb/search")
async def search_knowledge(request: KbSearchRequest):
    return await kb_service.search(request)


@app.get("/api/knowledge-base", response_model=list[KnowledgeDocument])
def list_knowledge_base():
    return kb_service.list_documents()


@app.post("/api/knowledge-base/import", response_model=KnowledgeImportResult)
async def import_knowledge_base(file: UploadFile = File(...)):
    filename = (file.filename or "").lower()
    if not filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="当前 MVP 先支持 CSV 导入，请上传 .csv 文件。")
    content = await file.read()
    try:
        result = kb_service.import_csv_text(content.decode("utf-8-sig"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return KnowledgeImportResult(**result)


@app.post("/api/knowledge-base/vector-index/rebuild", response_model=KbVectorIndexBuildResult)
async def rebuild_knowledge_vector_index() -> KbVectorIndexBuildResult:
    return KbVectorIndexBuildResult(**await kb_service.rebuild_vector_index())


@app.post("/internal/reply/generate")
async def generate_reply(request: ReplyGenerateRequest):
    return await reply_service.generate(
        request,
        prompt_overrides=store.get_system_config().get("prompts"),
        runtime_config=store.get_system_config(),
    )


@app.post("/internal/reply/check")
def check_reply(request: ReplyCheckRequest):
    return quality_service.check(request, config=store.get_system_config())


@app.post("/internal/agent/trace-preview", response_model=AgentTracePreviewResponse)
async def preview_agent_trace(request: AgentTracePreviewRequest) -> AgentTracePreviewResponse:
    require_llm_runtime(store.get_system_config())
    result = await orchestrator.multi_agent_runtime.run(
        request=request,
        conversation_id=request.conversation_id or "trace_preview",
        history=request.conversation_history,
        runtime_config=store.get_system_config(),
    )
    return AgentTracePreviewResponse(
        intent_result=result.intent_result,
        knowledge_hits=result.knowledge_hits,
        reply=result.reply,
        quality_check=result.quality_check,
        should_handoff=result.should_handoff,
        decision_reason=result.decision_reason,
        agent_plan=result.agent_plan,
        agent_trace=result.agent_trace,
    )


@app.get("/api/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str) -> ConversationDetail:
    detail = store.get_conversation_detail(conversation_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationDetail(**detail)


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str):
    deleted = store.delete_conversation(conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"deleted": True, "conversation_id": conversation_id}


@app.get("/api/conversations", response_model=list[ConversationListItem])
def list_conversations(
    status: str | None = Query(default=None),
    risk_level: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    return [ConversationListItem(**item) for item in store.list_conversations(status=status, risk_level=risk_level, limit=limit)]


@app.get("/api/follow-up/tasks")
def list_follow_up_tasks(status: str | None = Query(default=None)):
    return store.list_follow_up_tasks(status=status)


@app.post("/api/follow-up/tasks/cleanup")
def cleanup_follow_up_tasks():
    removed = store.cleanup_duplicate_follow_up_tasks()
    return {"removed": removed}


@app.post("/api/follow-up/tasks/clear-open")
def clear_open_follow_up_tasks():
    removed = store.clear_open_follow_up_tasks()
    return {"removed": removed}


@app.get("/api/follow-up/tasks/{task_id}")
def get_follow_up_task_detail(task_id: str):
    detail = store.get_follow_up_task_detail(task_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Task not found")
    return detail


@app.get("/api/dashboard/metrics", response_model=DashboardMetrics)
def get_dashboard_metrics() -> DashboardMetrics:
    return DashboardMetrics(**store.get_dashboard_metrics())


@app.get("/api/system/config", response_model=SystemConfig)
def get_system_config() -> SystemConfig:
    return SystemConfig(**store.get_system_config())


@app.patch("/api/system/config", response_model=SystemConfig)
def update_system_config(request: UpdateSystemConfigRequest) -> SystemConfig:
    return SystemConfig(**store.update_system_config(request.model_dump()))


@app.post("/api/follow-up/tasks/{task_id}/claim")
def claim_follow_up_task(task_id: str, request: ClaimTaskRequest):
    task = store.claim_task(task_id, request.assignee_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/api/follow-up/tasks/{task_id}/resolve")
def resolve_follow_up_task(task_id: str, request: ResolveTaskRequest):
    task = store.resolve_task(task_id, request.resolution_note)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/api/follow-up/tasks/{task_id}/manual-reply")
def manual_reply_follow_up_task(task_id: str, request: ManualFollowUpReplyRequest):
    task = store.resolve_task_with_manual_reply(task_id, request.manual_reply, request.resolution_note)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task
