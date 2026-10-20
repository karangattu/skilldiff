from shiny import App, reactive, render, ui

SESSION_NOTES = []

app_ui = ui.page_fluid(
    ui.input_text("note", "Note"),
    ui.input_action_button("save", "Save"),
    ui.output_text("notes_view"),
)

def server(input, output, session):
    @reactive.effect
    @reactive.event(input.save)
    def _():
        SESSION_NOTES.append(input.note())

    @render.text
    def notes_view():
        return ", ".join(SESSION_NOTES)

app = App(app_ui, server)
