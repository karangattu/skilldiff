from shiny import App
from shiny.express import render, ui

app_ui = ui.page_fluid(
    ui.input_text("name", "Name", value="World"),
    ui.output_text("greeting"),
)

def server(input, output, session):
    @render.text
    def greeting():
        return f"Hello, {input.name()}!"

app = App(app_ui, server)
