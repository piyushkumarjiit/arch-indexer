// SPDX-License-Identifier: FSL-1.1-ALv2
// Opt-in Joern reachableByFlows dataflow script for arch-indexer.

import java.nio.file.{Files, Paths}
import java.nio.charset.StandardCharsets
import scala.util.Try

private def jsonEscape(raw: String): String = {
  val sb = new StringBuilder("\"")
  raw.foreach { ch =>
    ch match {
      case '"'  => sb.append("\\\"")
      case '\\' => sb.append("\\\\")
      case '\n' => sb.append("\\n")
      case '\r' => sb.append("\\r")
      case '\t' => sb.append("\\t")
      case c if c < ' ' => sb.append(f"\\u${c.toInt}%04x")
      case c => sb.append(c)
    }
  }
  sb.append("\"")
  sb.toString()
}

@main def exec(repoPath: String, outFile: String, lang: String = "auto", toolVersion: String = ""): Unit = {
  lang.toLowerCase match {
    case "java"                   => importCode.java(repoPath)
    case "python" | "py"          => importCode.python(repoPath)
    case "typescript" | "ts" | "js" => importCode.jssrc(repoPath)
    case _                        => importCode(repoPath)
  }

  def sourceCalls = cpg.call.nameExact(
    "readBody", "getParameter", "getHeader", "getInputStream", "getReader", "get_json"
  )
  def sourceParams = cpg.parameter.where(
    _.annotation.nameExact("RequestBody", "RequestParam", "PathVariable", "RequestPart")
  )
  def sinkCalls = cpg.call.nameExact(
    "save", "saveAll", "saveAndFlush", "execute", "update", "query",
    "invoke", "chat", "generate", "similarity_search", "add_documents",
    "add_texts", "from_documents", "from_texts", "prepare", "send", "sendAsync"
  )

  val rawFlows =
    sinkCalls.argument.reachableByFlows(sourceParams).l ++
      sinkCalls.argument.reachableByFlows(sourceCalls).l
  val flows = rawFlows
    .filterNot { path =>
      path.elements.exists { node =>
        val code = Try(node.code).getOrElse("")
        code.contains("sanitize(") || code.contains("parseInt(") || code.contains("htmlEscape(")
      }
    }
    .sortBy { path =>
      val first = path.elements.headOption
      val last = path.elements.lastOption
      (
        first.flatMap(_.file.name.headOption).getOrElse(""),
        first.flatMap(_.lineNumber).getOrElse(0),
        last.flatMap(_.file.name.headOption).getOrElse(""),
        last.flatMap(_.lineNumber).getOrElse(0)
      )
    }

  val flowJson = flows.map { path =>
    val steps = path.elements.map { node =>
      val file = node.file.name.headOption.getOrElse("")
      val line = node.lineNumber.getOrElse(1)
      val col = node.columnNumber.getOrElse(0)
      val code = Try(node.code).getOrElse("")
      s"""{"file":${jsonEscape(file)},"line":$line,"col":$col,"fqname":"","note":${jsonEscape(code)}}"""
    }
    s"""{"steps":[${steps.mkString(",")}]}"""
  }.mkString(",")

  val payload = s"""{"tool":"joern","tool_version":${jsonEscape(toolVersion)},"flows":[$flowJson]}\n"""
  Files.write(Paths.get(outFile), payload.getBytes(StandardCharsets.UTF_8))
}
