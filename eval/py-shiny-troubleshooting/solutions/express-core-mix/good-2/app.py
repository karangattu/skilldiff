from shiny import App, reactive, render, ui

app_ui = ui.page_fluid(
    ui.input_text("name", "Name", value="World"),
    ui.output_text("greeting"),
)

def server(input, output, session):
    @reactive.calc
    def formatted():
        return f"Hello, {input.name()}!"

    @render.text
    def greeting():
        return formatted()

app = App(app_ui, server)
