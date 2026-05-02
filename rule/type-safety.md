# Type Safety

1. 用枚举/常量代替硬编码字符串（MessageRole、MessageType、FinishReason、DefaultValues 等）
2. 用结构体代替 dict（ChatMessage、ToolCall、LLMResponse、InputMessage、OutputMessage 等）
3. 函数签名必须写参数和返回值类型，禁止 bare Any / list / dict / list[Any]
4. 类设计必须做好抽象化, 禁止直接使用具体实现类, 而是应该使用抽象类或接口, 便于扩展/拔插/自定义
4. framework目录中的都是框架代码, examples中的都是业务代码使用示例, 必须按照业务需求和框架设计/通用性区分修改内容, 例如不能将业务代码中的配置写死到框架中