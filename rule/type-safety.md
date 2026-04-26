# Type Safety

1. 用枚举/常量代替硬编码字符串（MessageRole、MessageType、FinishReason、DefaultValues 等）
2. 用结构体代替 dict（ChatMessage、ToolCall、LLMResponse、InputMessage、OutputMessage 等）
3. 函数签名必须写参数和返回值类型，禁止 bare Any / list / dict / list[Any]
4. 类设计必须做好抽象化, 禁止直接使用具体实现类, 而是应该使用抽象类或接口, 便于扩展/拔插/自定义