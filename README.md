# ejudge.nvim

Плагин для Neovim: контесты Яндекс Кружка (ejudge.algocode.ru) без браузера. Скачивает условия и примеры, проверяет решение, отправляет его на сервер и показывает вердикты. Работает с теми же папками, что и [плагин для CLion](https://github.com/vmaiorov-collab/ejudge-clion), поэтому можно пользоваться обоими.

## Возможности

- `:EjudgeImport`: все контесты параллели с условиями (формулы переводятся в текст: `≤`, `10⁹`, `√(n)`), примерами и шаблоном `main.cpp`.
- `:EjudgeOpen`: выбор контеста и задачи. У задач видны вердикты: `✓` принята, `✗ WA` не принята. Условие открывается слева, решение рядом.
- `:EjudgeTest`: компиляция и проверка на примерах и ваших тестах. Для каждого теста время и память, предупреждение `⚠`, если вы близки к лимиту из условия.
- `:EjudgeRun`: запуск на своём вводе (`<Enter>` или `:w` в окне ввода). Третье окно для ожидаемого ответа.
- `:EjudgeSaveTest` и `:EjudgeDeleteTest`: свои тесты (`u1`, `u2`…) лежат рядом с примерами и проверяются каждый раз.
- `:EjudgeSubmit` и `:EjudgeTestSubmit` (сначала тесты, и только если все прошли, отправка).
- `:EjudgeSubmissions`: все посылки задачи с вердиктами, код любой открывается в окне.
- `require("ejudge").statusline()` для строки состояния: например `C ✗ WA · 10/19`.

## Установка

Нужны Neovim 0.10+, `python3` (только стандартная библиотека) и компилятор `c++` (на macOS: `xcode-select --install`).

lazy.nvim:

```lua
{
  "vmaiorov-collab/ejudge.nvim",
  cmd = { "EjudgeOpen", "EjudgeImport", "EjudgeLogin" },
  opts = {},
}
```

Без менеджера плагинов: `git clone https://github.com/vmaiorov-collab/ejudge.nvim ~/.local/share/nvim/site/pack/ejudge/start/ejudge.nvim`

## Начало работы

1. `:EjudgeLogin` и ввести логин и пароль от ejudge. Они сохраняются в файле `~/.local/share/nvim/ejudge/credentials.json` (читать его может только ваш пользователь). Либо задайте `$EJUDGE_LOGIN` и `$EJUDGE_PASSWORD`.
2. `:EjudgeImport` и выбрать параллель. Контесты скачиваются в `~/ejudge-contests`.
3. `:EjudgeOpen`: выбрать контест и задачу. Решайте в `main.cpp`.
4. `:EjudgeTest`, потом `:EjudgeSubmit`.

Выбор из списков идёт через `vim.ui.select`, так что с telescope-ui-select, fzf-lua или snacks работает поиск по задачам.

## Настройки

```lua
require("ejudge").setup({
  dir = "~/ejudge-contests",
  server = "https://ejudge.algocode.ru/cgi-bin/new-client",
  python = nil,           -- путь к python3, по умолчанию находится сам
  statement_width = 72,
  panel_height = 14,
  keymaps = false,        -- true: <leader>eo открыть, et тест, eT тест и отправка, er запуск, es отправить, eh посылки, ep условие
})
```

Для автодополнения clangd плагин кладёт `.clangd` и заголовок `bits/stdc++.h` в папку контестов.

## Как устроено

Lua отвечает за окна и команды, а `python/ejudge.py` ходит на сервер и запускает тесты. Ничего не отправляется никуда, кроме сервера ejudge и страницы algocode.ru со списком контестов. Статистику плагин не собирает.
