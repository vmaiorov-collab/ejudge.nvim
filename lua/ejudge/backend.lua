-- Runs python/ejudge.py and decodes its JSON lines.
local M = {}

local function plugin_root()
  local src = debug.getinfo(1, "S").source:sub(2)
  return vim.fs.dirname(vim.fs.dirname(vim.fs.dirname(src)))
end

---@param config table  plugin config (python, server, login, password)
---@param args string[] command and its arguments
---@param opts? {stdin?: string, on_progress?: fun(text: string), on_test?: fun(t: table), on_done: fun(ok: boolean, data: any)}
function M.run(config, args, opts)
  opts = opts or {}
  local python = config.python
  if not python or python == "" then
    python = vim.fn.exepath("python3") ~= "" and "python3" or "python"
  end
  if vim.fn.executable(python) ~= 1 then
    vim.schedule(function()
      opts.on_done(false, "Python 3 was not found. Install it or set `python = '/path/to/python3'` in setup()")
    end)
    return
  end
  local cmd = { python, plugin_root() .. "/python/ejudge.py", unpack(args) }
  local env = {
    EJUDGE_SERVER = config.server,
    EJUDGE_LOGIN = config.login or "",
    EJUDGE_PASSWORD = config.password or "",
    PYTHONIOENCODING = "utf-8",
  }
  local final, buffer, errors = nil, "", {}

  local function handle(line)
    if line == "" then return end
    local ok, msg = pcall(vim.json.decode, line)
    if not ok or type(msg) ~= "table" then return end
    if msg.type == "progress" and opts.on_progress then
      vim.schedule(function() opts.on_progress(msg.text) end)
    elseif msg.type == "test" and opts.on_test then
      vim.schedule(function() opts.on_test(msg) end)
    elseif msg.type == "result" then
      final = { true, msg.data }
    elseif msg.type == "error" then
      final = { false, msg.message }
    end
  end

  local ok, err = pcall(vim.system, cmd, {
    text = true,
    stdin = opts.stdin or "",
    env = env,
    stdout = function(_, data)
      if not data then return end
      buffer = buffer .. data
      while true do
        local nl = buffer:find("\n", 1, true)
        if not nl then break end
        handle(buffer:sub(1, nl - 1))
        buffer = buffer:sub(nl + 1)
      end
    end,
    stderr = function(_, data)
      if data then table.insert(errors, data) end
    end,
  }, function(res)
    handle(buffer)
    vim.schedule(function()
      if final then
        opts.on_done(final[1], final[2])
      else
        opts.on_done(false, #errors > 0 and table.concat(errors) or ("The backend exited with code " .. res.code))
      end
    end)
  end)
  if not ok then
    vim.schedule(function() opts.on_done(false, tostring(err)) end)
  end
end

return M
