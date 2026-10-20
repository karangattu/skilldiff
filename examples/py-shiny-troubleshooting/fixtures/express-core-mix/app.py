from shiny import App
from shiny.express import render, ui

ui.input_text("name", "Name", value="World")

app_ui = ui.page_fluid(
    ui.output_text("greeting"),
)

def server(input, output, session):
    @render.text
    def greeting():
        return f"Hello, {input.name()}!"

app = App(app_ui, server)
