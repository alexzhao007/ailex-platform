"""AiLex 自定义技能模板"""

async def ailex_skill(context, params):
    """AiLex 平台技能入口
    
    技能可以：
    - 调用万量引擎任意模型
    - 使用 OpenClaw 工具链
    - 访问本地文件系统
    - 执行 shell 命令
    """
    prompt = params.get("prompt", "")
    model = params.get("model", "gpt-4o")
    
    # 通过 gateway 调用模型
    result = await context.call_llm(
        model=model,
        messages=[{"role": "user", "content": prompt}]
    )
    
    return {"result": result}
