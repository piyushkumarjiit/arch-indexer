; Tier 2 fact captures for Java: method calls, constructor calls and annotations.
(method_invocation
  name: (_) @callee
  arguments: (argument_list) @args) @call

(object_creation_expression
  type: (_) @callee
  arguments: (argument_list) @args) @call

(annotation name: (_) @decorator) @decorator_node
(marker_annotation name: (_) @decorator) @decorator_node
