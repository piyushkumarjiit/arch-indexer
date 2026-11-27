; Tier 2 fact captures for Java: method calls, constructor calls, annotations and comparisons.
(method_invocation
  name: (_) @callee
  arguments: (argument_list) @args) @call

(object_creation_expression
  type: (_) @callee
  arguments: (argument_list) @args) @call

(annotation name: (_) @decorator) @decorator_node
(marker_annotation name: (_) @decorator) @decorator_node

(method_invocation
  object: (_) @lhs
  name: (identifier) @op
  arguments: (argument_list (_) @literal .)) @compare
